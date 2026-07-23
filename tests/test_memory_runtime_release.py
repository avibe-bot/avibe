from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from scripts import generate_memory_runtime_manifest as manifest_generator
from scripts.build_memory_runtime import LOCK_SHA256, PYTHON_VERSION, UV_VERSION, create_archive, prune_runtime


PLATFORMS = ("darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64")


def _write_archive(directory: Path, platform: str) -> tuple[Path, bytes]:
    binary = f"python-{platform}".encode()
    archive = directory / f"memory-runtime-1.1.3-{platform}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("bin/python")
        info.mode = 0o755
        info.size = len(binary)
        output.addfile(info, io.BytesIO(binary))
    metadata = {
        "platform": platform,
        "everos_version": "1.1.3",
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
    assert manifest["everos_version"] == "1.1.3"
    assert manifest["python_version"] == PYTHON_VERSION
    assert manifest["lock_sha256"] == LOCK_SHA256
    assert manifest["lock_id"] == f"uv-lock-sha256:{LOCK_SHA256}"
    assert manifest["uv_version"] == UV_VERSION
    assert manifest["provider_root_format"] == "everos-1.1.3"
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
    library.write_text("__version__ = '1.1.3'\n", encoding="utf-8")
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
    assert first_metadata["everos_version"] == "1.1.3"
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
    record = package.parent / "everos-1.1.3.dist-info" / "RECORD"
    record.parent.mkdir()
    record.write_text(
        "../../../bin/everos,sha256=random,1\n"
        "everos/__init__.py,sha256=stable,0\n"
        "everos-1.1.3.dist-info/RECORD,,\n",
        encoding="utf-8",
    )

    prune_runtime(runtime)

    assert binary.is_file()
    assert not tool.exists()
    assert not cache.exists()
    assert record.read_text(encoding="utf-8") == (
        "everos/__init__.py,sha256=stable,0\n"
        "everos-1.1.3.dist-info/RECORD,,\n"
    )
