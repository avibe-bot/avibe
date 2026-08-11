from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from core.memory.artifact import (
    FakeMemoryArtifactManager,
    MemoryArtifactManager,
    _sync_contract_from_payload,
)
from scripts import generate_memory_runtime_manifest, memory_runtime_release_guard
from scripts.build_memory_runtime import (
    EXPECTED_PLATFORMS,
    LOCK_SHA256,
    PYTHON_VERSION,
    SYNC_ARGV,
    SYNC_BOOTSTRAP_REVISION,
    UV_VERSION,
    create_archive,
    install_sync_bootstrap,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_contract(tmp_path: Path) -> tuple[Path, tuple[int, tuple[str, ...], str, str]]:
    root = tmp_path / "runtime"
    binary = root / "bin" / "python"
    site = root / "lib" / "python3.12" / "site-packages"
    binary.parent.mkdir(parents=True)
    site.mkdir(parents=True)
    binary.write_bytes(b"python")
    bootstrap = Path(__file__).parents[1] / "scripts" / "memory_runtime_sitecustomize.py"
    scrubbers = Path(__file__).parents[1] / "scripts" / "memory_runtime_sync_scrubbers.py"
    (site / "avibe_memory_sync_bootstrap.py").write_bytes(bootstrap.read_bytes())
    (site / "avibe_memory_sync_scrubbers.py").write_bytes(scrubbers.read_bytes())
    (site / "avibe_memory_sync_bootstrap.pth").write_text(
        "import avibe_memory_sync_bootstrap\n", encoding="ascii"
    )
    return binary, (
        1,
        ("-I", "-m", "everos.entrypoints.cli.main", "cascade", "sync"),
        _digest(bootstrap),
        _digest(scrubbers),
    )


def test_sync_admission_hashes_both_artifact_local_modules(tmp_path: Path) -> None:
    binary, contract = _runtime_contract(tmp_path)

    assert MemoryArtifactManager._admit_sync_contract(binary, contract)

    (binary.parent.parent / "lib" / "python3.12" / "site-packages" / "avibe_memory_sync_scrubbers.py").write_text(
        "tampered", encoding="ascii"
    )
    assert not MemoryArtifactManager._admit_sync_contract(binary, contract)


def test_sync_admission_rejects_missing_file_and_legacy_skips_capability(tmp_path: Path) -> None:
    binary, contract = _runtime_contract(tmp_path)
    site = binary.parent.parent / "lib" / "python3.12" / "site-packages"
    (site / "avibe_memory_sync_bootstrap.py").unlink()

    assert not MemoryArtifactManager._admit_sync_contract(binary, contract)
    assert MemoryArtifactManager._admit_sync_contract(binary, None)
    assert _sync_contract_from_payload({}) is None
    assert FakeMemoryArtifactManager().sync_capability() is False


def test_sync_manifest_fields_are_all_or_none() -> None:
    with pytest.raises(ValueError):
        _sync_contract_from_payload({"sync_bootstrap_revision": 1})


def test_sync_admission_is_pure_across_interleaved_contracts(tmp_path: Path) -> None:
    first_binary, first_contract = _runtime_contract(tmp_path / "first")
    second_binary, second_contract = _runtime_contract(tmp_path / "second")
    second_site = second_binary.parent.parent / "lib" / "python3.12" / "site-packages"
    (second_site / "avibe_memory_sync_scrubbers.py").write_text("tampered", encoding="ascii")

    assert MemoryArtifactManager._admit_sync_contract(first_binary, first_contract)
    assert not MemoryArtifactManager._admit_sync_contract(second_binary, second_contract)
    assert MemoryArtifactManager._admit_sync_contract(first_binary, first_contract)


def test_sync_admission_requires_one_exact_site_packages_location(tmp_path: Path) -> None:
    binary, contract = _runtime_contract(tmp_path)
    (binary.parent.parent / "lib" / "python3.11" / "site-packages").mkdir(parents=True)

    assert not MemoryArtifactManager._admit_sync_contract(binary, contract)


def test_release_guard_hashes_packaged_sync_modules(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    binary = runtime / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"python")
    binary.chmod(0o755)
    bootstrap_digest, scrubbers_digest = install_sync_bootstrap(runtime)
    scrubber = runtime / "lib" / "python3.12" / "site-packages" / "avibe_memory_sync_scrubbers.py"
    scrubber.write_text("tampered", encoding="ascii")
    builds = tmp_path / "builds"
    builds.mkdir()
    for platform in EXPECTED_PLATFORMS:
        archive = builds / f"memory-runtime-1.2.3-{platform}.tar.gz"
        metadata = create_archive(runtime_root=runtime, output=archive, platform=platform)
        metadata.update(
            {
                "python_version": PYTHON_VERSION,
                "lock_sha256": LOCK_SHA256,
                "uv_version": UV_VERSION,
                "sync_bootstrap_revision": SYNC_BOOTSTRAP_REVISION,
                "sync_bootstrap_sha256": bootstrap_digest,
                "sync_scrubbers_sha256": scrubbers_digest,
                "sync_argv": list(SYNC_ARGV),
            }
        )
        archive.with_suffix("").with_suffix(".json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
    manifest = tmp_path / "memory-runtime-manifest.json"
    generate_memory_runtime_manifest.build_manifest(
        archive_dir=builds,
        tag="v3.1.0",
        repo="avibe-bot/avibe",
        output=manifest,
    )
    assets = tmp_path / "assets"
    assets.mkdir()
    shutil.copy2(manifest, assets / "memory-runtime-manifest.json")
    for archive in builds.glob("*.tar.gz"):
        shutil.copy2(archive, assets / archive.name)

    verified = memory_runtime_release_guard.verify_release_assets(manifest, assets)
    assert verified.sync_scrubbers_sha256 == hashlib.sha256(b"tampered").hexdigest()
