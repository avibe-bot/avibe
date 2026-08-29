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
WORKFLOW_DIR = ROOT / ".github" / "workflows"


def _project(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


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


def test_memory_extra_and_distribution_share_one_compatibility_window() -> None:
    host = _project(ROOT / "pyproject.toml")
    memory = _project(MEMORY_PROJECT / "pyproject.toml")

    memory_requirement = _requirement(host["optional-dependencies"]["memory"], "avibe-memory")
    host_requirement = _requirement(memory["dependencies"], "avibe-os")

    assert memory_requirement.specifier == COMPATIBILITY
    assert host_requirement.specifier == COMPATIBILITY


def test_distribution_release_contract_is_forward_only_and_memory_first() -> None:
    contract = " ".join(
        (MEMORY_PROJECT / "README.md").read_text(encoding="utf-8").split()
    )

    assert "asset-complete GitHub Release" in contract
    assert "Publish `avibe-memory`" in contract
    assert "byte-identical to the staged wheel" in contract
    assert "Publish `avibe-os` only after that verification succeeds" in contract
    assert "forward-only" in contract
    assert "not release-ready by itself" not in contract
    assert "Wave 3b" not in contract
    assert "Wave 3c" not in contract


def test_release_workflows_have_no_unconditional_memory_wave_blocker() -> None:
    for name in ("publish.yml", "release_ai.yml"):
        workflow = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        assert "Block Memory Wave" not in workflow
        assert "Memory Wave 3a release blocked" not in workflow


@pytest.mark.parametrize(
    ("workflow_name", "job_name", "manifest_step"),
    [
        ("publish.yml", "build", "Verify package runtime manifests"),
        ("release_ai.yml", "build-assets", "Verify bundled runtime manifests"),
    ],
)
def test_release_paths_build_and_verify_both_distributions(
    workflow_name: str,
    job_name: str,
    manifest_step: str,
) -> None:
    job = _workflow(workflow_name)["jobs"][job_name]
    names = [step.get("name") for step in job["steps"]]
    pin = _step(job, "Pin package version to release tag")["run"]
    build = _step(job, "Build Python distributions")["run"]
    assets = _step(job, "Verify Python distribution asset matrix")["run"]
    manifests = _step(job, manifest_step)["run"]
    package_matrix = _step(job, "Verify distribution package matrix")["run"]

    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_OS=$PACKAGE_VERSION" in pin
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_MEMORY=$PACKAGE_VERSION" in pin
    assert "python -m build --outdir dist packaging/avibe-memory" in build
    assert "python -m build" in build
    for pattern in (
        "avibe_memory-*.whl",
        "avibe_memory-*.tar.gz",
        "avibe_os-*.whl",
        "avibe_os-*.tar.gz",
    ):
        assert pattern in assets
    assert 'AVIBE_CORE_WHEEL="$(ls dist/avibe_os-*.whl)"' in package_matrix
    assert 'AVIBE_MEMORY_WHEEL="$(ls dist/avibe_memory-*.whl)"' in package_matrix
    assert "pytest tests/test_memory_distribution.py" in package_matrix
    assert 'Path("dist").glob("avibe_os-*.whl")' in manifests or "avibe_os-" in manifests
    assert "avibe_memory-" in manifests
    assert "avibe-os wheel unexpectedly owns the Memory Runtime manifest" in manifests
    assert "avibe-memory wheel is missing vibe/memory_runtime_manifest.json" in manifests
    assert names.index("Build Python distributions") < names.index(manifest_step)
    assert names.index(manifest_step) < names.index("Verify distribution package matrix")


def test_github_only_release_uploads_both_distribution_pairs() -> None:
    workflow = _workflow("release_ai.yml")
    build_job = workflow["jobs"]["build-assets"]
    release_job = workflow["jobs"]["release"]
    artifact_upload = _step(build_job, "Upload release artifacts")
    release_upload = _step(release_job, "Create GitHub-only Release")["run"]

    assert artifact_upload["with"]["path"] == "dist/"
    assert "for path in dist/*" in release_upload
    assert 'package_assets+=("$path")' in release_upload
    assert 'gh release upload "$TAG"' in release_upload
    assert '"${package_assets[@]}" --clobber' in release_upload


def test_official_draft_uploads_both_verified_distribution_pairs() -> None:
    build_job = _workflow("publish.yml")["jobs"]["build"]
    names = [step.get("name") for step in build_job["steps"]]
    release_upload = _step(build_job, "Upload GitHub release assets")["run"]

    assert names.index("Verify Python distribution asset matrix") < names.index(
        "Upload GitHub release assets"
    )
    assert 'gh release upload "$TAG" --repo "${GITHUB_REPOSITORY}" dist/* --clobber' in release_upload


def test_official_release_publishes_and_verifies_memory_before_core() -> None:
    jobs = _workflow("publish.yml")["jobs"]

    assert {"build", "finalize-github-release"} <= _needs(
        jobs["publish-avibe-memory"]
    )
    assert jobs["publish-avibe-memory"]["environment"] == "pypi-avibe-memory"
    assert "publish-avibe-memory" in _needs(jobs["verify-avibe-memory-pypi"])
    assert "verify-avibe-memory-pypi" in _needs(jobs["publish-avibe-os"])
    assert "finalize-github-release" in _needs(jobs["publish-avibe-os"])

    verify = _step(
        jobs["verify-avibe-memory-pypi"],
        "Verify PyPI serves the exact avibe-memory wheel",
    )["run"]
    for fragment in (
        "for attempt in {1..12}",
        "python -m pip --isolated download",
        "--index-url https://pypi.org/simple",
        "--no-deps",
        "--no-cache-dir",
        "--only-binary=:all:",
        'cmp -s "${staged_wheels[0]}" "${public_wheels[0]}"',
        "PyPI already serves a non-identical avibe-memory wheel",
    ):
        assert fragment in verify


@pytest.mark.parametrize(
    ("job_name", "keep_step", "allowed", "excluded"),
    [
        (
            "publish-avibe-memory",
            "Keep avibe-memory distributions only",
            "avibe_memory-*",
            "avibe_os-*",
        ),
        (
            "publish-avibe-os",
            "Keep avibe-os distributions only",
            "avibe_os-*",
            "avibe_memory-*",
        ),
        (
            "publish-vibe-remote",
            "Keep vibe-remote distributions only",
            "vibe_remote-*",
            "avibe_",
        ),
    ],
)
def test_each_trusted_publisher_receives_only_its_distribution(
    job_name: str,
    keep_step: str,
    allowed: str,
    excluded: str,
) -> None:
    job = _workflow("publish.yml")["jobs"][job_name]
    names = [step.get("name") for step in job["steps"]]
    keep = _step(job, keep_step)["run"]
    publish_steps = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("pypa/gh-action-pypi-publish@")
    ]

    assert len(publish_steps) == 1
    assert names.index(keep_step) < job["steps"].index(publish_steps[0])
    assert allowed in keep
    assert excluded not in keep
    assert publish_steps[0]["with"]["skip-existing"] is True


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
    environment = tmp_path / installation
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
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
