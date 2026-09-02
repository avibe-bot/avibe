from __future__ import annotations

import base64
import csv
from email.parser import Parser
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from typing import Any
import zipfile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
import pytest
import yaml

from hatch_exact_peer import pin_peer_dependency

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
MEMORY_PROJECT = ROOT / "packaging" / "avibe-memory"
COMPATIBILITY = SpecifierSet(">=3.0.14.dev0,<3.1")
PACKAGE_CONTRACT_VERSION = Version("3.0.99rc1")
WORKFLOW_DIR = ROOT / ".github" / "workflows"


def _project(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def _build_system(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["build-system"]


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
        metadata_content = archive.read(metadata_paths[0])
        metadata = Parser().parsestr(metadata_content.decode("utf-8"))
        record_paths = [name for name in names if name.endswith(".dist-info/RECORD")]
        assert len(record_paths) == 1
        record = list(csv.reader(io.StringIO(archive.read(record_paths[0]).decode("utf-8"))))
        metadata_record = [row for row in record if row and row[0] == metadata_paths[0]]
        assert len(metadata_record) == 1
        digest = base64.urlsafe_b64encode(hashlib.sha256(metadata_content).digest()).rstrip(b"=").decode("ascii")
        assert metadata_record[0][1:] == [f"sha256={digest}", str(len(metadata_content))]
    return names, metadata


def _sdist_metadata(wheel: Path, distribution: str) -> Any:
    sdist = _sdist_path(wheel, distribution)
    with tarfile.open(sdist, "r:gz") as archive:
        metadata = _sdist_member(archive, "PKG-INFO")
        return Parser().parsestr(metadata.decode("utf-8"))


def _sdist_pyproject(wheel: Path, distribution: str) -> dict[str, Any]:
    sdist = _sdist_path(wheel, distribution)
    with tarfile.open(sdist, "r:gz") as archive:
        return tomllib.loads(_sdist_member(archive, "pyproject.toml").decode("utf-8"))


def _sdist_path(wheel: Path, distribution: str) -> Path:
    matches = list(wheel.parent.glob(f"{distribution}-*.tar.gz"))
    assert len(matches) == 1
    return matches[0]


def _sdist_member(archive: tarfile.TarFile, name: str) -> bytes:
    matches = [
        member
        for member in archive.getmembers()
        if member.isfile() and Path(member.name).name == name
    ]
    assert len(matches) == 1
    member_file = archive.extractfile(matches[0])
    assert member_file is not None
    return member_file.read()


def _write_minimal_wheel(directory: Path, distribution: str, version: str) -> Path:
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    wheel = directory / f"{normalized}-{version}-py3-none-any.whl"
    entries = {
        f"{dist_info}/METADATA": f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n\n",
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: avibe-package-contract-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n\n"
        ),
    }
    entries[f"{dist_info}/RECORD"] = "".join(f"{name},,\n" for name in entries) + f"{dist_info}/RECORD,,\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return wheel


def _parent_site_packages() -> list[Path]:
    paths = [
        path
        for value in sys.path
        if value and (path := Path(value).resolve()).name in {"site-packages", "dist-packages"}
    ]
    assert paths
    return paths


def _provision_build_requirement_wheelhouse(pyprojects: tuple[dict[str, Any], ...], directory: Path) -> None:
    requirements = sorted(
        {
            value
            for pyproject in pyprojects
            for value in pyproject["build-system"]["requires"]
        }
    )
    parsed = [Requirement(value) for value in requirements]
    directory.mkdir()
    _run(
        Path(sys.executable),
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--dest",
        str(directory),
        *requirements,
        cwd=directory.parent,
    )

    wheels = list(directory.glob("*.whl"))
    assert wheels
    downloaded_names = {canonicalize_name(parse_wheel_filename(wheel.name)[0]) for wheel in wheels}
    assert {canonicalize_name(requirement.name) for requirement in parsed} <= downloaded_names


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


def test_source_metadata_preserves_the_developer_compatibility_window() -> None:
    host = _project(ROOT / "pyproject.toml")
    memory = _project(MEMORY_PROJECT / "pyproject.toml")

    memory_requirement = _requirement(host["optional-dependencies"]["memory"], "avibe-memory")
    host_requirement = _requirement(memory["dependencies"], "avibe-os")

    assert memory_requirement.specifier == COMPATIBILITY
    assert host_requirement.specifier == COMPATIBILITY


def test_sdist_rewriter_is_an_explicit_isolated_build_requirement() -> None:
    for pyproject in (ROOT / "pyproject.toml", MEMORY_PROJECT / "pyproject.toml"):
        requirement = _requirement(_build_system(pyproject)["requires"], "tomlkit")
        assert str(requirement.specifier) == ">=0.11.1"


def test_pr_artifact_build_uses_an_explicit_prerelease_contract_version() -> None:
    step = _step(_workflow("lint.yml")["jobs"]["build-linux-artifacts"], "Build package artifact")
    environment = step["env"]

    assert Version(environment["AVIBE_PACKAGE_CONTRACT_VERSION"]) == PACKAGE_CONTRACT_VERSION
    assert PACKAGE_CONTRACT_VERSION.is_prerelease
    assert environment["SETUPTOOLS_SCM_PRETEND_VERSION"] == str(PACKAGE_CONTRACT_VERSION)
    assert environment["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_OS"] == str(PACKAGE_CONTRACT_VERSION)
    assert environment["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_MEMORY"] == str(PACKAGE_CONTRACT_VERSION)
    assert "python -m build\n" in step["run"]


def test_publishable_metadata_without_the_peer_contract_fails_closed(tmp_path: Path) -> None:
    wheel = tmp_path / "avibe_os-3.0.99rc1-py3-none-any.whl"
    dist_info = "avibe_os-3.0.99rc1.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.4\nName: avibe-os\nVersion: 3.0.99rc1\n\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            f"{dist_info}/METADATA,,\n{dist_info}/RECORD,,\n",
        )

    with pytest.raises(RuntimeError, match="exactly one avibe-memory dependency"):
        pin_peer_dependency(
            str(wheel),
            project_name="avibe-os",
            peer_name="avibe-memory",
            package_version="3.0.99rc1",
            peer_extra="memory",
        )

    assert not wheel.exists()


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


def test_built_distributions_have_independent_contents_and_exact_peer_metadata() -> None:
    core_wheel = _wheel_path("AVIBE_CORE_WHEEL")
    memory_wheel = _wheel_path("AVIBE_MEMORY_WHEEL")
    core_names, core_metadata = _wheel_metadata(core_wheel)
    memory_names, memory_metadata = _wheel_metadata(memory_wheel)
    core_sdist_metadata = _sdist_metadata(core_wheel, "avibe_os")
    memory_sdist_metadata = _sdist_metadata(memory_wheel, "avibe_memory")
    core_sdist_pyproject = _sdist_pyproject(core_wheel, "avibe_os")
    memory_sdist_pyproject = _sdist_pyproject(memory_wheel, "avibe_memory")
    core_sdist_project = core_sdist_pyproject["project"]
    memory_sdist_project = memory_sdist_pyproject["project"]

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

    core_version = Version(core_metadata["Version"])
    memory_version = Version(memory_metadata["Version"])
    assert memory_version == core_version
    explicit_version = os.environ.get("AVIBE_PACKAGE_CONTRACT_VERSION")
    if explicit_version is not None:
        assert core_version == Version(explicit_version) == PACKAGE_CONTRACT_VERSION

    for metadata in (core_metadata, core_sdist_metadata):
        assert Version(metadata["Version"]) == core_version
        memory_requirement = _requirement(metadata.get_all("Requires-Dist") or [], "avibe-memory")
        assert str(memory_requirement.specifier) == f"=={core_version}"
        assert memory_requirement.marker is not None
        assert memory_requirement.marker.evaluate({"extra": "memory"})
        assert not memory_requirement.marker.evaluate({"extra": "not-memory"})
    for metadata in (memory_metadata, memory_sdist_metadata):
        assert Version(metadata["Version"]) == memory_version
        host_requirement = _requirement(metadata.get_all("Requires-Dist") or [], "avibe-os")
        assert str(host_requirement.specifier) == f"=={memory_version}"
        assert host_requirement.marker is None

    core_build_requirement = _requirement(core_sdist_project["optional-dependencies"]["memory"], "avibe-memory")
    memory_build_requirement = _requirement(memory_sdist_project["dependencies"], "avibe-os")
    assert str(core_build_requirement.specifier) == f"=={core_version}"
    assert core_build_requirement.marker is None
    assert str(memory_build_requirement.specifier) == f"=={memory_version}"
    assert memory_build_requirement.marker is None
    for pyproject in (core_sdist_pyproject, memory_sdist_pyproject):
        build_requirement = _requirement(pyproject["build-system"]["requires"], "tomlkit")
        assert str(build_requirement.specifier) == ">=0.11.1"


def _assert_core_distribution_mirrors_builtin_skills(
    distribution: Path,
    *,
    environment: Path,
    tmp_path: Path,
) -> None:
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(
        python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        str(distribution),
        cwd=tmp_path,
    )
    avibe_home = tmp_path / "avibe-home"
    result = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from core.managed_skills import "
                "builtin_skills_source, load_skill, prepare_builtin_skills; "
                "source = builtin_skills_source(); "
                "assert source.name == 'builtin_skills_source'; "
                "snapshot = prepare_builtin_skills(); "
                "skill = load_skill('use-avibe'); "
                "assert skill is not None and skill.body; "
                "assert skill.directory.name == 'use-avibe'; "
                "print(snapshot)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "AVIBE_HOME": str(avibe_home)},
        cwd=tmp_path,
    )

    snapshot_id = result.stdout.strip()
    assert len(snapshot_id) == 64
    expected = {
        path.relative_to(ROOT / "skills").as_posix()
        for path in (ROOT / "skills").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    mirrored = {
        path.relative_to(avibe_home / "builtin-skills" / snapshot_id).as_posix()
        for path in (avibe_home / "builtin-skills" / snapshot_id).rglob("*")
        if path.is_file()
    }
    assert mirrored == expected
    if os.name != "nt":
        for relative in expected:
            source_mode = (ROOT / "skills" / relative).stat().st_mode & 0o111
            mirrored_mode = (
                avibe_home / "builtin-skills" / snapshot_id / relative
            ).stat().st_mode & 0o111
            assert mirrored_mode == source_mode


def test_core_wheel_installs_and_mirrors_builtin_skills_without_a_checkout(tmp_path: Path) -> None:
    core_wheel = _wheel_path("AVIBE_CORE_WHEEL")
    _assert_core_distribution_mirrors_builtin_skills(
        core_wheel,
        environment=tmp_path / "builtin-skills-wheel",
        tmp_path=tmp_path,
    )


def test_core_sdist_installs_and_mirrors_builtin_skills_without_a_checkout(tmp_path: Path) -> None:
    core_sdist = _sdist_path(_wheel_path("AVIBE_CORE_WHEEL"), "avibe_os")
    _assert_core_distribution_mirrors_builtin_skills(
        core_sdist,
        environment=tmp_path / "builtin-skills-sdist",
        tmp_path=tmp_path,
    )


def test_memory_extra_resolves_and_installs_the_same_version_pair(tmp_path: Path) -> None:
    core_wheel = _wheel_path("AVIBE_CORE_WHEEL")
    memory_wheel = _wheel_path("AVIBE_MEMORY_WHEEL")
    core_version = Version(_wheel_metadata(core_wheel)[1]["Version"])
    environment = tmp_path / "same-version-resolver"
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    child_site_packages = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    Path(child_site_packages, "avibe-test-environment.pth").write_text(
        "".join(f"{path}\n" for path in _parent_site_packages()),
        encoding="utf-8",
    )
    _run(
        python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-deps",
        "--force-reinstall",
        str(core_wheel),
        cwd=tmp_path,
    )
    _run(
        python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--find-links",
        str(core_wheel.parent),
        "--find-links",
        str(memory_wheel.parent),
        f"avibe-os[memory]=={core_version}",
        cwd=tmp_path,
    )
    _run(
        python,
        "-c",
        (
            "from importlib.metadata import version; "
            f"assert version('avibe-os') == version('avibe-memory') == {str(core_version)!r}"
        ),
        cwd=tmp_path,
    )


def test_memory_extra_resolves_and_installs_from_both_sdists(tmp_path: Path) -> None:
    core_wheel = _wheel_path("AVIBE_CORE_WHEEL")
    memory_wheel = _wheel_path("AVIBE_MEMORY_WHEEL")
    core_sdist = _sdist_path(core_wheel, "avibe_os")
    memory_sdist = _sdist_path(memory_wheel, "avibe_memory")
    core_pyproject = _sdist_pyproject(core_wheel, "avibe_os")
    memory_pyproject = _sdist_pyproject(memory_wheel, "avibe_memory")
    core_version = Version(_sdist_metadata(core_wheel, "avibe_os")["Version"])
    decoy_version = Version("3.0.99rc2")
    assert decoy_version > core_version

    package_links = tmp_path / "package-links"
    package_links.mkdir()
    shutil.copy2(memory_sdist, package_links / memory_sdist.name)
    _write_minimal_wheel(package_links, "avibe-memory", str(decoy_version))
    build_links = tmp_path / "build-links"
    _provision_build_requirement_wheelhouse((core_pyproject, memory_pyproject), build_links)

    environment = tmp_path / "sdist-resolver"
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    child_site_packages = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    Path(child_site_packages, "avibe-test-runtime-dependencies.pth").write_text(
        "".join(f"{path}\n" for path in _parent_site_packages()),
        encoding="utf-8",
    )

    install_command = [
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--find-links",
        str(package_links),
        "--find-links",
        str(build_links),
        f"avibe-os[memory] @ {core_sdist.as_uri()}",
    ]
    assert "--no-build-isolation" not in install_command
    assert install_command.count("--find-links") == 2
    _run(
        python,
        *install_command,
        cwd=tmp_path,
    )
    _run(
        python,
        "-c",
        (
            "from importlib.metadata import version; "
            f"assert version('avibe-os') == version('avibe-memory') == {str(core_version)!r}"
        ),
        cwd=tmp_path,
    )


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
            "assert not hasattr(loader, 'MEMORY_RUNTIME_PROTOCOL_VERSION'); "
            "assert not hasattr(loader, 'MEMORY_RUNTIME_LIFECYCLE_CONTRACT')"
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
