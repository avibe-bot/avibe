from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from scripts import build_memory_runtime as runtime_builder
from scripts import generate_memory_runtime_manifest as manifest_generator
from scripts.build_memory_runtime import (
    EVEROS_VERSION,
    EXPECTED_PLATFORMS as BUILD_PLATFORMS,
    LOCK_SHA256,
    PYTHON_VERSION,
    UV_VERSION,
    _short_socket_directory,
    _temporary_directory,
    create_archive,
    prune_runtime,
)


PLATFORMS = ("darwin-arm64", "linux-arm64", "linux-x64")


def test_memory_runtime_release_platform_contract_excludes_darwin_x64() -> None:
    expected = set(PLATFORMS)

    assert BUILD_PLATFORMS == expected
    assert manifest_generator.EXPECTED_PLATFORMS == expected


def test_release_workflows_emit_metadata_for_the_current_runtime_version() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    expected = f'memory-runtime-{EVEROS_VERSION}-${{{{ matrix.artifact }}}}.json'

    for name in ("release_ai.yml", "publish.yml"):
        workflow = (workflows / name).read_text(encoding="utf-8")
        assert expected in workflow
        assert "memory-runtime-1.2.1-${{ matrix.artifact }}.json" not in workflow


def test_release_workflows_build_memory_runtime_under_runner_temp() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"

    for name in ("release_ai.yml", "publish.yml"):
        workflow = (workflows / name).read_text(encoding="utf-8")
        build_step = workflow.split("- name: Build Memory Runtime bundle", 1)[1]
        build_step = build_step.split("- name: Upload Memory Runtime bundle", 1)[0]
        assert "TMPDIR: ${{ runner.temp }}" in build_step


def test_memory_runtime_scratch_paths_resolve_the_system_temp_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_temp = tmp_path / "real-temp"
    real_temp.mkdir()
    linked_temp = tmp_path / "linked-temp"
    linked_temp.symlink_to(real_temp, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(linked_temp))

    with _temporary_directory("memory-runtime-test-") as scratch:
        assert scratch.parent == real_temp.resolve(strict=True)
        assert scratch == scratch.resolve(strict=True)


def test_memory_runtime_health_home_uses_a_short_canonical_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_temp = tmp_path / ("long-temp-" * 12)
    long_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(long_temp))

    with _short_socket_directory("mrv-") as health_home:
        socket_path = health_home / "memory" / ".rt" / "everos.sock"
        assert health_home.parent == Path("/tmp").resolve(strict=True)
        assert health_home == health_home.resolve(strict=True)
        assert len(os.fsencode(socket_path)) < 104


def test_memory_runtime_sidecar_smoke_claims_its_provider_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def capture_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runtime_builder.subprocess, "run", capture_run)

    runtime_builder._sidecar_health_smoke(
        Path(sys.executable),
        effective_home=tmp_path,
    )

    assert captured[4:] == [str(sys.executable), str(tmp_path), EVEROS_VERSION]
    tree = ast.parse(captured[3])
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    process_call = next(
        call
        for call in calls
        if isinstance(call.func, ast.Name) and call.func.id == "EverOSProcess"
    )
    ensure_call = next(
        call
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "ensure"
    )
    guard = next(
        keyword.value
        for keyword in process_call.keywords
        if keyword.arg == "provider_root_guard"
    )
    assert ast.unparse(ensure_call.func.value) == "provider_root"
    assert ensure_call.lineno < process_call.lineno
    assert isinstance(guard, ast.Lambda)
    assert isinstance(guard.body, ast.Call)
    assert isinstance(guard.body.func, ast.Attribute)
    assert ast.unparse(guard.body.func.value) == "provider_root"
    assert guard.body.func.attr == "require_owned"
    assert [ast.unparse(argument) for argument in guard.body.args] == [
        "provider_root_meta",
        "provider_root_metadata",
    ]
    socket_keyword = next(
        keyword.value
        for keyword in process_call.keywords
        if keyword.arg == "socket_path"
    )
    assert ast.unparse(socket_keyword) == "socket_path"
    assert "process.socket_path" not in captured[3]
    assert "process.last_error" not in captured[3]


def test_github_only_release_runs_memory_runtime_guard_before_uploading_assets() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/release_ai.yml"
    ).read_text(encoding="utf-8")

    guard = workflow.index("Run Memory Runtime release guard for GitHub-only assets")
    upload = workflow.index("Add Show Runtime release assets")
    guarded_section = workflow[guard:upload]

    assert '[[ "$TAG" != gh-v* ]]' in guarded_section
    assert "scripts/memory_runtime_release_guard.py" in guarded_section
    assert "verify --asset-dir memory-release-guard-assets" in guarded_section


def _write_archive(directory: Path, platform: str) -> tuple[Path, bytes]:
    binary = f"python-{platform}".encode()
    archive = directory / f"memory-runtime-1.2.3-{platform}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("bin/python")
        info.mode = 0o755
        info.size = len(binary)
        output.addfile(info, io.BytesIO(binary))
    metadata = {
        "platform": platform,
        "everos_version": "1.2.3",
        "python_version": PYTHON_VERSION,
        "lock_sha256": LOCK_SHA256,
        "uv_version": UV_VERSION,
        "name": archive.name,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(binary).hexdigest(),
        "size": archive.stat().st_size,
        "bin_path": "bin/python",
    }
    archive.with_suffix("").with_suffix(".json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    return archive, binary


def test_generate_memory_runtime_manifest_records_verified_platform_archives(tmp_path: Path) -> None:
    expected: dict[str, tuple[Path, bytes]] = {
        platform: _write_archive(tmp_path, platform) for platform in PLATFORMS
    }
    output = tmp_path / "memory-runtime-manifest.json"

    manifest = manifest_generator.build_manifest(
        archive_dir=tmp_path,
        tag="v3.1.0",
        repo="avibe-bot/avibe",
        output=output,
    )

    assert output.is_file()
    assert manifest["release_state"] == "published"
    assert manifest["release_tag"] == "v3.1.0"
    assert manifest["everos_version"] == "1.2.3"
    assert manifest["python_version"] == PYTHON_VERSION
    assert manifest["lock_sha256"] == LOCK_SHA256
    assert manifest["lock_id"] == f"uv-lock-sha256:{LOCK_SHA256}"
    assert manifest["uv_version"] == UV_VERSION
    assert manifest["provider_root_format"] == "everos-1.2.3"
    assert manifest["compatible_provider_root_formats"] == []
    assert set(manifest["archives"]) == set(PLATFORMS)
    for platform, (archive, binary) in expected.items():
        item = manifest["archives"][platform]
        assert item == {
            "name": archive.name,
            "url": f"https://github.com/avibe-bot/avibe/releases/download/v3.1.0/{archive.name}",
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "binary_sha256": hashlib.sha256(binary).hexdigest(),
            "size": archive.stat().st_size,
            "bin_path": "bin/python",
        }


def test_build_outputs_metadata_accepted_by_manifest_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if command == ["uv", "--version"]:
            stdout = f"uv {UV_VERSION}\n"
        elif command[:3] == ["uv", "python", "install"]:
            binary = cwd / "managed" / "bin" / "python"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"embedded-python")
            binary.chmod(0o755)
            stdout = ""
        elif command[:3] == ["uv", "python", "find"]:
            stdout = f"{cwd / 'managed' / 'bin' / 'python'}\n"
        elif command[:2] == ["uv", "export"]:
            output = Path(command[command.index("--output-file") + 1])
            output.write_text("", encoding="utf-8")
            stdout = ""
        elif command[:3] == ["uv", "pip", "sync"]:
            stdout = ""
        elif command[1:4] == ["-I", "-B", "-c"]:
            stdout = f"{EVEROS_VERSION}\n{PYTHON_VERSION}\n"
        elif command[1:] == ["-c", "import platform; print(platform.python_version())"]:
            stdout = f"{PYTHON_VERSION}\n"
        else:
            raise AssertionError(f"Unexpected Memory Runtime subprocess: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(runtime_builder, "_run", fake_run)
    monkeypatch.setattr(runtime_builder, "_sidecar_health_smoke", lambda *args, **kwargs: None)

    for platform in PLATFORMS:
        metadata_path = tmp_path / f"memory-runtime-{EVEROS_VERSION}-{platform}.json"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "build_memory_runtime.py",
                "--output-dir",
                str(tmp_path),
                "--platform",
                platform,
                "--metadata-output",
                str(metadata_path),
            ],
        )

        assert runtime_builder.main() == 0
        output = json.loads(capsys.readouterr().out)

        assert set(output) == {"ok", "archive", "metadata"}
        assert json.loads(metadata_path.read_text(encoding="utf-8")) == output["metadata"]

    manifest = manifest_generator.build_manifest(
        archive_dir=tmp_path,
        tag="v3.1.0",
        repo="avibe-bot/avibe",
        output=tmp_path / "manifest.json",
    )

    assert set(manifest["archives"]) == set(PLATFORMS)


def test_generate_memory_runtime_manifest_fails_when_platform_archive_missing(tmp_path: Path) -> None:
    for platform in PLATFORMS[:-1]:
        _write_archive(tmp_path, platform)

    with pytest.raises(SystemExit, match="linux-x64"):
        manifest_generator.build_manifest(
            archive_dir=tmp_path,
            tag="v3.1.0",
            repo="avibe-bot/avibe",
            output=tmp_path / "manifest.json",
        )


def test_generate_memory_runtime_manifest_rejects_oversized_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for platform in PLATFORMS:
        _write_archive(tmp_path, platform)
    monkeypatch.setattr(manifest_generator, "MAX_ARCHIVE_BYTES", 1)

    with pytest.raises(SystemExit, match="1 GiB"):
        manifest_generator.build_manifest(
            archive_dir=tmp_path,
            tag="v3.1.0",
            repo="avibe-bot/avibe",
            output=tmp_path / "manifest.json",
        )


def test_create_memory_runtime_archive_is_deterministic_and_has_install_layout(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    binary = runtime / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"embedded-python")
    binary.chmod(0o755)
    library = runtime / "lib" / "python3.12" / "site-packages" / "everos" / "__init__.py"
    library.parent.mkdir(parents=True)
    library.write_text("__version__ = '1.2.3'\n", encoding="utf-8")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    first_metadata = create_archive(runtime_root=runtime, output=first, platform="darwin-arm64")
    os.utime(binary, (2_000_000_000, 2_000_000_000))
    os.utime(library, (2_000_000_001, 2_000_000_001))
    second_metadata = create_archive(runtime_root=runtime, output=second, platform="darwin-arm64")

    assert first.read_bytes() == second.read_bytes()
    assert first_metadata["sha256"] == second_metadata["sha256"]
    assert first_metadata["binary_sha256"] == hashlib.sha256(b"embedded-python").hexdigest()
    assert first_metadata["platform"] == "darwin-arm64"
    assert first_metadata["everos_version"] == "1.2.3"
    assert first_metadata["bin_path"] == "bin/python"
    with tarfile.open(first, "r:gz") as archive:
        names = archive.getnames()
        assert "bin/python" in names
        assert "lib/python3.12/site-packages/everos/__init__.py" in names
        assert archive.getmember("bin/python").mode == 0o755
        assert archive.getmember("bin/python").mtime == 0


def test_create_memory_runtime_archive_rejects_symlinks(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    binary = runtime / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"embedded-python")
    binary.chmod(0o755)
    (runtime / "bin" / "python3").symlink_to("python")

    with pytest.raises(SystemExit, match="must not contain symlinks"):
        create_archive(
            runtime_root=runtime,
            output=tmp_path / "runtime.tar.gz",
            platform="darwin-arm64",
        )


def test_create_memory_runtime_archive_rejects_darwin_x64(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="Unsupported Memory Runtime platform: darwin-x64"):
        create_archive(
            runtime_root=tmp_path / "unused",
            output=tmp_path / "runtime.tar.gz",
            platform="darwin-x64",
        )


def test_prune_memory_runtime_removes_generated_paths_and_updates_records(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    binary = runtime / "bin" / "python"
    tool = runtime / "bin" / "everos"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"python")
    binary.chmod(0o755)
    tool.write_text(f"#!{binary}\n", encoding="utf-8")
    package = runtime / "lib" / "python3.12" / "site-packages" / "everos"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312.pyc").write_bytes(b"random-build-path")
    record = package.parent / "everos-1.2.3.dist-info" / "RECORD"
    record.parent.mkdir()
    record.write_text(
        "../../../bin/everos,sha256=random,1\n"
        "everos/__init__.py,sha256=stable,0\n"
        "everos-1.2.3.dist-info/RECORD,,\n",
        encoding="utf-8",
    )

    prune_runtime(runtime)

    assert binary.is_file()
    assert not tool.exists()
    assert not cache.exists()
    assert record.read_text(encoding="utf-8") == (
        "everos/__init__.py,sha256=stable,0\n"
        "everos-1.2.3.dist-info/RECORD,,\n"
    )
