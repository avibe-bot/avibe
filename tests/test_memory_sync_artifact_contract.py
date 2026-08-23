from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from core.managed_runtime import ManagedRuntimeArchive, ManagedRuntimeManifest, runtime_platform_tag
from core.memory.artifact import (
    ARTIFACT_ADMISSION_REVISION,
    EMBEDDED_PYTHON_VERSION,
    EVEROS_VERSION,
    PACKAGE_LOCK_SHA256,
    RUNTIME_BUILDER_UV_VERSION,
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


def _runtime_contract(
    tmp_path: Path,
    *,
    runtime_root: Path | None = None,
) -> tuple[Path, tuple[int, tuple[str, ...], str, str]]:
    root = runtime_root or tmp_path / "runtime"
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


def test_sync_capability_rejects_a_nonadmitted_active_pointer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)
    manifest_sha256 = "a" * 64
    archive_sha256 = "b" * 64
    platform = runtime_platform_tag()
    fingerprint = manager._legacy_artifact_fingerprint(manifest_sha256, archive_sha256)
    install_dir = manager.runtime_dir / "versions" / EVEROS_VERSION / platform / fingerprint
    binary, contract = _runtime_contract(tmp_path, runtime_root=install_dir)
    binary.chmod(0o755)
    pointer = {
        "provider": "manifest",
        "runtime_id": manager.spec.runtime_id,
        "runtime_version": EVEROS_VERSION,
        "platform": platform,
        "install_dir": str(install_dir),
        "manifest_sha256": manifest_sha256,
        "archive_sha256": archive_sha256,
        "bin_path": "bin/python",
        "admission_revision": ARTIFACT_ADMISSION_REVISION,
        "admission_ok": False,
        "sync_bootstrap_revision": contract[0],
        "sync_argv": list(contract[1]),
        "sync_bootstrap_sha256": contract[2],
        "sync_scrubbers_sha256": contract[3],
    }
    (install_dir / manager.spec.metadata_filename).write_text(
        json.dumps({**pointer, "binary_sha256": _digest(binary)}),
        encoding="utf-8",
    )
    manager._restore_current_pointer(pointer)

    assert manager.installed_artifact_snapshot().admission == "broken"
    assert manager.sync_capability() is False


def test_sync_corruption_breaks_snapshot_even_when_core_admission_cache_hits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)
    manifest_sha256 = "a" * 64
    archive_sha256 = "b" * 64
    platform = runtime_platform_tag()
    fingerprint = manager._legacy_artifact_fingerprint(manifest_sha256, archive_sha256)
    install_dir = manager.runtime_dir / "versions" / EVEROS_VERSION / platform / fingerprint
    binary, contract = _runtime_contract(tmp_path, runtime_root=install_dir)
    binary.chmod(0o755)
    pointer = {
        "provider": "manifest",
        "runtime_id": manager.spec.runtime_id,
        "runtime_version": EVEROS_VERSION,
        "platform": platform,
        "install_dir": str(install_dir),
        "manifest_sha256": manifest_sha256,
        "archive_sha256": archive_sha256,
        "bin_path": "bin/python",
        "provider_root_format": "everos-1.2.3",
        "compatible_provider_root_formats": [],
        "artifact_fingerprint": "released-artifact",
        "sync_bootstrap_revision": contract[0],
        "sync_argv": list(contract[1]),
        "sync_bootstrap_sha256": contract[2],
        "sync_scrubbers_sha256": contract[3],
    }
    (install_dir / manager.spec.metadata_filename).write_text(
        json.dumps({**pointer, "binary_sha256": _digest(binary)}),
        encoding="utf-8",
    )
    manager._restore_current_pointer(pointer)
    probes: list[Path] = []

    def admit(candidate: Path) -> dict[str, bool]:
        probes.append(candidate)
        return {"ok": True}

    monkeypatch.setattr(manager, "_prepare_binary", admit)
    assert manager.installed_artifact_snapshot().admission == "ok"
    assert probes == [binary]
    assert list((manager.runtime_dir / "derived" / "admission").glob("*.json"))

    scrubbers = (
        install_dir
        / "lib"
        / "python3.12"
        / "site-packages"
        / "avibe_memory_sync_scrubbers.py"
    )
    scrubbers.write_text("tampered", encoding="ascii")

    snapshot = manager.installed_artifact_snapshot()
    assert snapshot.admission == "broken"
    assert manager.sync_capability() is False
    assert probes == [binary]


def test_fresh_install_admits_the_manifest_sync_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    binary_payload = b"python"
    archive_path = tmp_path / "memory-runtime.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive_file:
        binary = tarfile.TarInfo("bin/python")
        binary.mode = 0o755
        binary.size = len(binary_payload)
        archive_file.addfile(binary, io.BytesIO(binary_payload))

    expected_contract = (
        SYNC_BOOTSTRAP_REVISION,
        tuple(SYNC_ARGV),
        "a" * 64,
        "b" * 64,
    )
    archive = ManagedRuntimeArchive(
        platform=runtime_platform_tag(),
        name=archive_path.name,
        url=archive_path.as_uri(),
        sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        binary_sha256=hashlib.sha256(binary_payload).hexdigest(),
        size=archive_path.stat().st_size,
        bin_path="bin/python",
    )
    manifest = ManagedRuntimeManifest(
        schema_version=1,
        runtime_version=EVEROS_VERSION,
        source="avibe-bot/avibe",
        source_url=None,
        archives={archive.platform: archive},
        digest="c" * 64,
        loaded_from=str(tmp_path / "manifest.json"),
        payload={
            "release_state": "published",
            "python_version": EMBEDDED_PYTHON_VERSION,
            "lock_sha256": PACKAGE_LOCK_SHA256,
            "lock_id": f"uv-lock-sha256:{PACKAGE_LOCK_SHA256}",
            "uv_version": RUNTIME_BUILDER_UV_VERSION,
            "provider_root_format": "everos-1.2.3",
            "compatible_provider_root_formats": ["everos-1.2.3"],
            "sync_bootstrap_revision": expected_contract[0],
            "sync_argv": list(expected_contract[1]),
            "sync_bootstrap_sha256": expected_contract[2],
            "sync_scrubbers_sha256": expected_contract[3],
        },
    )
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        provider_root=tmp_path / "memory" / "everos-root",
        offline=True,
    )
    prepared: list[Path] = []
    admitted: list[tuple[int, tuple[str, ...], str, str] | None] = []

    monkeypatch.setattr(manager, "_load_manifest", lambda *, allow_network: manifest)
    monkeypatch.setattr(manager, "_resolve_manifest_archive", lambda _archive: archive_path)
    monkeypatch.setattr(manager, "_binary_matches_manifest", lambda *_args: True)
    monkeypatch.setattr(manager, "_write_current_pointer", lambda *_args: None)

    def prepare(binary: Path) -> dict[str, bool]:
        assert binary.name == "python"
        prepared.append(binary)
        return {"ok": True}

    def admit_sync(
        binary: Path,
        contract: tuple[int, tuple[str, ...], str, str] | None,
    ) -> bool:
        assert binary.name == "python"
        admitted.append(contract)
        return contract == expected_contract

    monkeypatch.setattr(manager, "_prepare_binary", prepare)
    monkeypatch.setattr(manager, "_admit_sync_contract", admit_sync)

    assert manager.ensure()["ok"] is True
    assert len(prepared) == 1
    assert admitted == [expected_contract]


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
