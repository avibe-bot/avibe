"""Release pins must survive legacy resolvers and an sdist -> wheel rebuild.

Existing manifest packaging tests do not exercise dependency metadata. These
checks protect the artifact boundary, not a mock of Hatch's build-data handling.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import shutil
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from scripts.release_dependency_constraints import (
    CONSTRAINTS_PATH,
    check_constraints,
    export_constraints,
    pinned_requirements,
)

ROOT = Path(__file__).resolve().parents[1]


def _lock_copy(tmp_path: Path) -> Path:
    for name in ("pyproject.toml", "uv.lock", CONSTRAINTS_PATH.as_posix()):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return tmp_path


def test_committed_constraints_are_current_runtime_only_with_hashes():
    content = check_constraints(ROOT)
    pins = pinned_requirements(content)
    names = {canonicalize_name(Requirement(pin).name) for pin in pins}
    assert {"httpcore", "hpack", "socksio", "pydantic-core"} <= names
    assert not {"avibe-os", "pytest", "pytest-asyncio", "iniconfig", "pluggy"} & names
    assert content.count("--hash=sha256:") > len(pins)
    lock_digest = hashlib.sha256((ROOT / "uv.lock").read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert f"# uv.lock SHA256 (LF): {lock_digest}" in content


def test_windows_checkout_line_endings_do_not_change_constraints(tmp_path):
    root = _lock_copy(tmp_path)
    for path in (root / "uv.lock", root / CONSTRAINTS_PATH):
        path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    assert check_constraints(root) == check_constraints(ROOT)


@pytest.mark.parametrize("damage", ["missing", "pin", "hash", "lock"])
def test_stale_constraints_fail_closed(tmp_path, damage):
    root = _lock_copy(tmp_path)
    artifact = root / CONSTRAINTS_PATH
    if damage == "missing":
        artifact.unlink()
    elif damage == "pin":
        artifact.write_text(artifact.read_text().replace("httpcore==1.0.9", "httpcore==0.0.0"))
    elif damage == "hash":
        artifact.write_text(artifact.read_text().replace("--hash=sha256:", "--hash=sha256:0", 1))
    else:
        with (root / "uv.lock").open("a") as stream:
            stream.write("\n# Lock changed, even if only dev dependencies changed.\n")
    with pytest.raises(RuntimeError, match="Missing or stale"):
        check_constraints(root)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('"anyio>=4.16.0",', '"anyio>=4.17.0",'),
        ('"anyio>=4.16.0",', '"anyio>=4.16.0", "new-runtime-dependency>=1.0",'),
        ('"anyio>=4.16.0",', ''),
        ("SQLAlchemy>=2.1.0; python_version >= '3.11'", "SQLAlchemy>=2.1.0; python_version >= '3.12'"),
        ('"httpx[http2,socks]>=0.27"', '"httpx[http2,socks,brotli]>=0.27"'),
        ('requires-python = ">=3.10"', 'requires-python = ">=3.11"'),
    ],
    ids=["range", "new-runtime", "removed-runtime", "marker", "extras", "python-range"],
)
def test_source_drift_cannot_be_blessed_by_frozen_export(tmp_path, before, after):
    root = _lock_copy(tmp_path)
    project = root / "pyproject.toml"
    original = project.read_text(encoding="utf-8")
    assert before in original
    project.write_text(original.replace(before, after), encoding="utf-8")
    constraints = (root / CONSTRAINTS_PATH).read_bytes()
    lock = (root / "uv.lock").read_bytes()
    with pytest.raises(RuntimeError, match="runtime requirements do not match uv.lock"):
        check_constraints(root)
    assert (root / CONSTRAINTS_PATH).read_bytes() == constraints
    assert (root / "uv.lock").read_bytes() == lock


def test_equivalent_requirement_syntax_and_dev_only_drift_are_allowed(tmp_path):
    root = _lock_copy(tmp_path)
    project = root / "pyproject.toml"
    original = project.read_text(encoding="utf-8")
    project.write_text(
        original.replace("sys_platform == 'darwin' and platform_machine == 'x86_64'",
                         "platform_machine == 'x86_64' and sys_platform == 'darwin'")
        .replace("SQLAlchemy>=2.0.57,<2.1", "sqlalchemy<2.1,>=2.0.57")
        .replace("pytest>=9.0.3", "pytest>=9.1.0"),
        encoding="utf-8",
    )
    assert check_constraints(root) == check_constraints(ROOT)


@pytest.mark.parametrize("requirement", ["package>=1.0", "package==1.*", "package @ https://example.com/a.whl", ""])
def test_release_pins_reject_non_exact_or_empty_graph(requirement):
    with pytest.raises(ValueError):
        pinned_requirements(requirement)


def _assert_universal_contract(requirements):
    parsed = [Requirement(item) for item in requirements]
    for python, platform, machine, sqlalchemy, sdk, crypto in [
        ("3.10", "linux", "x86_64", "2.0.57", "0.2.162", "50.0.2"),
        ("3.11", "linux", "x86_64", "2.1.0", "0.2.162", "50.0.2"),
        ("3.13", "win32", "AMD64", "2.1.0", "0.2.159", "50.0.2"),
        ("3.14", "darwin", "x86_64", "2.1.0", "0.2.162", "48.0.1"),
        ("3.14", "darwin", "arm64", "2.1.0", "0.2.162", "50.0.2"),
        ("3.15", "linux", "x86_64", "2.1.0", "0.2.162", "50.0.2"),
        ("3.15", "win32", "AMD64", "2.1.0", "0.2.159", "50.0.2"),
        ("3.15", "darwin", "x86_64", "2.1.0", "0.2.162", "48.0.1"),
        ("3.15", "darwin", "arm64", "2.1.0", "0.2.162", "50.0.2"),
    ]:
        environment = {
            "python_version": python,
            "python_full_version": f"{python}.1",
            "sys_platform": platform,
            "platform_machine": machine,
            "platform_python_implementation": "CPython",
            "implementation_name": "cpython",
        }
        active = [item for item in parsed if item.marker is None or item.marker.evaluate(environment)]
        for name, expected in {"sqlalchemy": sqlalchemy, "claude-agent-sdk": sdk, "cryptography": crypto}.items():
            matching = [item for item in active if canonicalize_name(item.name) == name]
            exact = {specifier.version for item in matching for specifier in item.specifier if specifier.operator == "=="}
            assert exact == {expected}, (environment, name, matching)
            assert all(expected in item.specifier for item in matching)
        assert any(canonicalize_name(item.name) == "tomli" for item in active) == (python == "3.10")
        assert any(canonicalize_name(item.name) == "pywin32" for item in active) == (platform == "win32")


def test_export_keeps_universal_python_os_and_architecture_forks():
    _assert_universal_contract(pinned_requirements(check_constraints(ROOT)))


def _build_source(tmp_path: Path) -> Path:
    root = _lock_copy(tmp_path / "source")
    for name in (
        "hatch_build.py", "README.md", "LICENSE",
        "scripts/release_dependency_constraints.py", "scripts/show_runtime_manifest_asset.py",
        "scripts/release_asset_download.py", "scripts/release_package_version.py",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    # Payload content is unrelated to the metadata contract; use a tiny valid
    # source tree but the real project configuration, hooks, lock, and exporter.
    for name in ("vibe/__init__.py", "main.py", "skills/example/SKILL.md", "ui/dist/index.html", "vibe/show_router.tsx"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fixture\n")
    runtime = "a" * 40
    platforms = ("darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64", "win32-arm64", "win32-x64")
    manifest = {
        "schema_version": 1,
        "runtime_version": runtime,
        "runtime_source": {"repo": "avibe-bot/vibe-show-runtime", "ref": runtime},
        "minimum_node": ">=22.12.0",
        "archives": {
            platform: {
                "name": f"vibe-show-runtime-node-{platform}.tgz",
                "url": f"https://github.com/avibe-bot/avibe/releases/download/v3.0.8/vibe-show-runtime-node-{platform}.tgz",
                "sha256": "b" * 64,
                "size": 100,
            }
            for platform in platforms
        },
    }
    (root / "vibe/show_runtime_manifest.json").write_text(json.dumps(manifest))
    return root


def _wheel_metadata(path):
    with zipfile.ZipFile(path) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        return BytesParser().parsebytes(archive.read(metadata)), archive.read(CONSTRAINTS_PATH.as_posix())


def test_wheel_and_rebuilt_sdist_pin_graph_without_changing_source_or_editable(tmp_path, monkeypatch):
    # Build requirements are intentionally not runtime/dev dependencies. The
    # focused release gate installs them before selecting this test.
    pytest.importorskip("hatchling")
    pytest.importorskip("hatch_vcs")
    pytest.importorskip("tomlkit")
    pytest.importorskip("editables")
    from hatchling.builders.sdist import SdistBuilder
    from hatchling.builders.wheel import WheelBuilder

    monkeypatch.setenv("SETUPTOOLS_SCM_PRETEND_VERSION", "9.9.9")
    root = _build_source(tmp_path)
    original_project = (root / "pyproject.toml").read_bytes()
    original_lock = (root / "uv.lock").read_bytes()
    source_dependencies = tomllib.loads(original_project.decode())["project"]["dependencies"]
    constraints = (root / CONSTRAINTS_PATH).read_bytes()
    # Independent parser for the export's requirement lines, rather than using
    # the implementation's pin parser to decide which metadata must exist.
    expected_pins = {
        str(Requirement(line.removesuffix("\\").strip()))
        for line in constraints.decode().splitlines()
        if line and not line.startswith(("#", " "))
    }
    wheel_path = next(WheelBuilder(str(root)).build(directory=str(tmp_path / "wheel"), versions=["standard"]))
    wheel_metadata, wheel_constraints = _wheel_metadata(wheel_path)
    assert wheel_constraints == constraints
    wheel_requirements = wheel_metadata.get_all("Requires-Dist")
    assert expected_pins <= {str(Requirement(item)) for item in wheel_requirements}
    _assert_universal_contract(wheel_requirements)
    for name, extras in {"httpx": {"http2", "socks"}, "pyjwt": {"crypto"}, "uvicorn": {"standard"}}.items():
        assert any(canonicalize_name(Requirement(item).name) == name and Requirement(item).extras == extras
                   for item in wheel_requirements)
    supported_python = SpecifierSet(wheel_metadata["Requires-Python"])
    assert all(version in supported_python for version in ("3.10", "3.14", "3.15"))

    sdist_path = next(SdistBuilder(str(root)).build(directory=str(tmp_path / "sdist"), versions=["standard"]))
    with tarfile.open(sdist_path) as archive:
        names = archive.getnames()
        prefix = names[0].split("/")[0]
        assert archive.extractfile(f"{prefix}/{CONSTRAINTS_PATH.as_posix()}").read() == constraints
        sdist_metadata = BytesParser().parsebytes(archive.extractfile(f"{prefix}/PKG-INFO").read())
        archive.extractall(tmp_path / "unpacked", filter="data")
    unpacked = tmp_path / "unpacked" / prefix
    archived_dependencies = tomllib.loads((unpacked / "pyproject.toml").read_text())["project"]["dependencies"]
    assert expected_pins <= {str(Requirement(item)) for item in archived_dependencies}
    assert set(sdist_metadata.get_all("Requires-Dist")) == set(wheel_requirements)
    # Frozen export from the *rewritten* sdist must still match the original
    # graph, without Git, the original checkout, a resolver, or network access.
    assert export_constraints(unpacked).encode() == constraints
    rebuilt_path = next(WheelBuilder(str(unpacked)).build(directory=str(tmp_path / "rebuilt"), versions=["standard"]))
    rebuilt_metadata, rebuilt_constraints = _wheel_metadata(rebuilt_path)
    assert set(rebuilt_metadata.get_all("Requires-Dist")) == set(wheel_requirements)
    assert rebuilt_metadata["Requires-Python"] == wheel_metadata["Requires-Python"]
    assert rebuilt_constraints == constraints

    # Metadata-only frontends can bypass build hooks. The archived pyproject
    # must already contain the complete pins before prepare_metadata runs.
    monkeypatch.chdir(unpacked)
    from hatchling.build import prepare_metadata_for_build_wheel
    metadata_dir = tmp_path / "prepared"
    metadata_dir.mkdir()
    prepared = prepare_metadata_for_build_wheel(str(metadata_dir))
    prepared_metadata = BytesParser().parsebytes((metadata_dir / prepared / "METADATA").read_bytes())
    assert set(prepared_metadata.get_all("Requires-Dist")) == set(wheel_requirements)

    assert (root / "pyproject.toml").read_bytes() == original_project
    assert (root / "uv.lock").read_bytes() == original_lock
    # Editable builds need neither generated release artifacts nor uv export.
    (root / CONSTRAINTS_PATH).unlink()
    (root / "uv.lock").unlink()
    editable_path = next(WheelBuilder(str(root)).build(directory=str(tmp_path / "editable"), versions=["editable"]))
    with zipfile.ZipFile(editable_path) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        editable_metadata = BytesParser().parsebytes(archive.read(metadata))
    assert {Requirement(item) for item in editable_metadata.get_all("Requires-Dist")} == {
        Requirement(item) for item in source_dependencies
    }


@pytest.mark.parametrize("mode", ["development", "stable", "preview", "local-tag"])
def test_distributions_keep_portable_version_frozen_description_and_original_readme(tmp_path, monkeypatch, mode):
    """Protect PyPI's actual artifact description, including an offline rebuild.

    Relative images/file links broke on PyPI even though README worked on GitHub.
    Dependency packaging coverage above never inspects the description payload.
    """
    pytest.importorskip("hatchling")
    pytest.importorskip("hatch_vcs")
    pytest.importorskip("tomlkit")
    from hatchling.builders.sdist import SdistBuilder
    from hatchling.builders.wheel import WheelBuilder

    version = "9.9.9rc1" if mode == "preview" else "9.9.9"
    ref = {"development": "master", "stable": "v9.9.9", "preview": "gh-v9.9.9rc1", "local-tag": "v9.9.9"}[mode]
    monkeypatch.setenv("SETUPTOOLS_SCM_PRETEND_VERSION", version)
    monkeypatch.delenv("AVIBE_BUILD_TAG", raising=False)
    root = _build_source(tmp_path)
    if mode == "local-tag":
        for args in (["init"], ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "fixture"], ["tag", ref]):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    elif mode != "development":
        monkeypatch.setenv("AVIBE_BUILD_TAG", ref)
    # Exercise the real README plus Markdown/HTML forms it might acquire. The
    # inputs remain relative: the fixture must not supply the hook's output.
    with (root / "README.md").open("a", encoding="utf-8") as stream:
        stream.write('''
![Markdown image](assets/test.png)
[![Linked image](assets/badge.png)](LICENSE)
[File with title](./docs/CLI.md "CLI")
[Angle destination](<docs/CLI.md>)
<a href='SECURITY.md'>Security</a><img src=assets/unquoted.png>
[Reference link][manual]
![Reference image][picture]
[manual]: docs/CLI.md "CLI"
[picture]: assets/reference.png
[Anchor](#mac) [Email](mailto:test@example.com)
`[not a link](leave-relative.md)`
```
<img src="leave-relative.png">
```
''')
    original_readme = (root / "README.md").read_bytes()
    original_project = (root / "pyproject.toml").read_bytes()
    wheel = next(WheelBuilder(str(root)).build(directory=str(tmp_path / "wheel"), versions=["standard"]))
    metadata, _ = _wheel_metadata(wheel)
    description = metadata.get_payload()
    assert metadata["Description-Content-Type"] == "text/markdown"
    raw_base = f"https://raw.githubusercontent.com/avibe-bot/avibe/{ref}/"
    file_base = f"https://github.com/avibe-bot/avibe/blob/{ref}/"
    for image in ("assets/logo.png", "assets/test.png", "assets/badge.png", "assets/unquoted.png", "assets/reference.png"):
        assert raw_base + image in description
    for file in ("README.md", "README_ZH.md", "LICENSE", "SECURITY.md", "docs/CLI.md"):
        assert file_base + file in description
    assert '[Anchor](#mac) [Email](mailto:test@example.com)' in description
    assert '`[not a link](leave-relative.md)`' in description
    assert '```\n<img src="leave-relative.png">\n```' in description
    # Inspect every prose destination, not just the currently known screenshots.
    prose = description.replace('`[not a link](leave-relative.md)`', '').replace('```\n<img src="leave-relative.png">\n```', '')
    destinations = re.findall(r'''\]\(<?([^\s)>]+)|(?:src|href)\s*=\s*['"]?([^\s'">]+)|^\[[^\]]+\]:\s*<?([^\s>]+)''', prose, re.MULTILINE)
    assert destinations
    for groups in destinations:
        url = next(value for value in groups if value)
        assert url.startswith(("https://", "http://", "#", "mailto:", "//")), url

    sdist = next(SdistBuilder(str(root)).build(directory=str(tmp_path / "sdist"), versions=["standard"]))
    with tarfile.open(sdist) as archive:
        prefix = archive.getnames()[0].split("/")[0]
        assert archive.extractfile(f"{prefix}/README.md").read() == original_readme
        sdist_metadata = BytesParser().parsebytes(archive.extractfile(f"{prefix}/PKG-INFO").read())
        assert sdist_metadata.get_payload() == description
        archive.extractall(tmp_path / "unpacked", filter="data")
    unpacked = tmp_path / "unpacked" / prefix
    monkeypatch.delenv("AVIBE_BUILD_TAG", raising=False)
    rebuilt = next(WheelBuilder(str(unpacked)).build(directory=str(tmp_path / "rebuilt"), versions=["standard"]))
    assert _wheel_metadata(rebuilt)[0].get_payload() == description
    # PEP 517 metadata preparation may bypass the build hook entirely.
    from hatchling.build import prepare_metadata_for_build_wheel
    monkeypatch.chdir(unpacked)
    prepared_dir = tmp_path / "prepared"
    prepared_dir.mkdir()
    prepared = prepare_metadata_for_build_wheel(str(prepared_dir))
    prepared_metadata = BytesParser().parsebytes((prepared_dir / prepared / "METADATA").read_bytes())
    assert prepared_metadata.get_payload() == description
    assert (root / "README.md").read_bytes() == original_readme
    assert (root / "pyproject.toml").read_bytes() == original_project


def test_description_rejects_release_tag_for_a_different_version(tmp_path, monkeypatch):
    pytest.importorskip("hatchling")
    pytest.importorskip("hatch_vcs")
    from hatchling.builders.wheel import WheelBuilder

    monkeypatch.setenv("SETUPTOOLS_SCM_PRETEND_VERSION", "9.9.9")
    monkeypatch.setenv("AVIBE_BUILD_TAG", "v1.2.3")
    root = _build_source(tmp_path)
    with pytest.raises(ValueError, match="does not match package version"):
        next(WheelBuilder(str(root)).build(directory=str(tmp_path / "wheel"), versions=["standard"]))


@pytest.mark.parametrize("damage", ["artifact", "source-range", "source-new-runtime"])
def test_build_hook_rejects_stale_artifact(tmp_path, monkeypatch, damage):
    pytest.importorskip("hatchling")
    pytest.importorskip("hatch_vcs")
    from hatchling.builders.wheel import WheelBuilder

    monkeypatch.setenv("SETUPTOOLS_SCM_PRETEND_VERSION", "9.9.9")
    root = _build_source(tmp_path)
    if damage == "artifact":
        (root / CONSTRAINTS_PATH).write_text("httpcore==0.0.0\n")
        error = "Missing or stale"
    else:
        project = root / "pyproject.toml"
        replacement = ('"anyio>=4.17.0",' if damage == "source-range"
                       else '"anyio>=4.16.0", "new-runtime-dependency>=1.0",')
        project.write_text(project.read_text().replace('"anyio>=4.16.0",', replacement))
        error = "runtime requirements do not match uv.lock"
    with pytest.raises(RuntimeError, match=error):
        next(WheelBuilder(str(root)).build(directory=str(tmp_path / "wheel"), versions=["standard"]))
