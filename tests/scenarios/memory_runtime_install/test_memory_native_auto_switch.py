"""Wake crosses real archive, active-pointer, and data-root boundaries.

Only native execution and remote provider responses are deterministic fakes.
No released archive, real model credential, or current user data is consumed.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from avibe_memory import artifact as artifact_module
from avibe_memory import runtime as runtime_module
from avibe_memory.artifact import MemoryArtifactManager
from avibe_memory.artifact_contract import ColdArtifactAdmissionResult
from avibe_memory.everos import FakeMemoryProvider
from avibe_memory.process import FakeEverOSProcess, FakeEverOSProcessFactory
from config.v2_config import MemoryConfig, MemoryEndpointConfig, MemoryProcessingConfig
from core import managed_runtime
from vibe import api


def _release(tmp_path: Path, name: str, *, root_format: str = "everos-1.2.3") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    binary = f"same-version native bytes: {name}".encode()
    archive = directory / "memory-runtime-1.2.3-linux-arm64.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        entry = tarfile.TarInfo("bin/python")
        entry.mode = 0o755
        entry.size = len(binary)
        bundle.addfile(entry, io.BytesIO(binary))
    manifest = directory / "memory-runtime-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "everos_version": "1.2.3",
        "python_version": "3.12.12",
        "lock_sha256": artifact_module.PACKAGE_LOCK_SHA256,
        "lock_id": f"uv-lock-sha256:{artifact_module.PACKAGE_LOCK_SHA256}",
        "uv_version": artifact_module.RUNTIME_BUILDER_UV_VERSION,
        "source": name,
        "release_state": "published",
        "provider_root_format": root_format,
        "compatible_provider_root_formats": [],
        "archives": {
            "linux-arm64": {
                "name": archive.name,
                "url": archive.as_uri(),
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "binary_sha256": hashlib.sha256(binary).hexdigest(),
                "size": archive.stat().st_size,
                "bin_path": "bin/python",
            },
        },
    }), encoding="utf-8")
    return manifest


@pytest.fixture
def released_runtime(tmp_path, monkeypatch, memory_runtime_factory):
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "linux-arm64")
    monkeypatch.setattr(artifact_module, "runtime_platform_tag", lambda: "linux-arm64")
    monkeypatch.setattr(
        artifact_module, "run_cold_artifact_admission",
        lambda _binary: ColdArtifactAdmissionResult(ok=True, reason=None, duration_ms=1),
    )
    monkeypatch.setattr(
        runtime_module, "EverOSPort", lambda *args, **kwargs: FakeMemoryProvider(),
    )
    home = tmp_path / "isolated-home"
    config = MemoryConfig(
        enabled=True,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.test/v1", "chat", "fixture-key"),
            embedding=MemoryEndpointConfig("https://embed.test/v1", "embed", "fixture-key"),
        ),
    )

    def make(manifest):
        manager = MemoryArtifactManager(
            runtime_dir=home / "runtime" / "memory",
            manifest_path=manifest,
            provider_root=home / "memory" / "everos-root",
        )
        # These are execution probes, not archive/pointer/root validation.
        monkeypatch.setattr(manager, "_admit_error_scrubbers", lambda _binary: None)
        monkeypatch.setattr(manager, "_binary_matches_manifest", lambda *_args: True)
        processes = FakeEverOSProcessFactory()
        runtime = memory_runtime_factory(
            config, artifact_manager=manager, process_factory=processes,
            effective_home=home,
        )
        return runtime, manager, processes

    return make, home, config


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ("startup", "running-wake"))
@pytest.mark.parametrize("change", ("archive", "contract"))
async def test_wake_converges_same_version_release_bytes_without_changing_user_state(
    tmp_path, monkeypatch, memory_runtime_factory, released_runtime, entrypoint, change,
):
    """MEMORY-RUNTIME-INSTALL-004: enabled upgrade converges without a config toggle."""

    make, home, config = released_runtime
    old_manifest = _release(tmp_path, "old")
    new_manifest = _release(tmp_path, "new")
    if change == "contract":
        payload = json.loads(old_manifest.read_text())
        payload["compatible_provider_root_formats"] = ["historical-compatible-format"]
        new_manifest.write_text(json.dumps(payload), encoding="utf-8")
    runtime, manager, processes = make(old_manifest)
    assert await runtime.wake() == {"ok": True, "state": "running"}
    old_python = manager.resolve_python()
    old_fingerprint = manager.artifact_fingerprint()
    root = home / "memory" / "everos-root"
    sentinel = root / "user-记忆.txt"
    sentinel.write_text("保留原有记忆 café 🌱", encoding="utf-8")
    before_root = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    before_config = asdict(config)

    if entrypoint == "startup":
        await memory_runtime_factory.close(runtime)
        assert not any(process.running for process in processes.created)
        runtime, manager, processes = make(new_manifest)
    else:
        # Simulate selecting the new package manifest while the old native runs.
        monkeypatch.setattr(manager, "manifest_path", new_manifest)
    assert manager.status()["version"] == "1.2.3"
    assert manager.status()["matches_manifest"] is False
    ensured = []
    ensure = manager.ensure

    def install(*, force=False):
        assert not any(process.running for process in processes.created)
        ensured.append(force)
        return ensure(force=force)

    monkeypatch.setattr(manager, "ensure", install)
    assert await runtime.wake() == {"ok": True, "state": "running"}
    assert ensured == [True]
    assert manager.status()["version"] == "1.2.3"
    assert manager.status()["matches_manifest"] is True
    assert manager.resolve_python() != old_python  # Forced admission stages a sibling.
    assert manager.artifact_fingerprint() != old_fingerprint
    assert old_python.is_file()  # Previous admitted generation is retained.
    assert manager.resolve_python().read_bytes() == (
        b"same-version native bytes: new" if change == "archive" else b"same-version native bytes: old"
    )
    assert sum(process.running for process in processes.created) == 1
    assert processes.supervised[-1].python == manager.resolve_python()
    assert asdict(config) == before_config
    assert runtime._config.enabled and runtime._wake_config.enabled
    for name, content in before_root.items():
        assert (root / name).read_bytes() == content

    assert await runtime.wake() == {"ok": True, "state": "running"}
    assert ensured == [True]  # Already-current Wake must not reinstall.


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("download", "digest", "admission", "activation", "root"))
async def test_failed_update_resumes_only_the_retained_admitted_artifact(
    tmp_path, monkeypatch, memory_runtime_factory, released_runtime, failure,
):
    """MEMORY-RUNTIME-INSTALL-005: rejected candidates preserve existing data/runtime."""

    make, home, config = released_runtime
    old_manifest = _release(tmp_path, "old")
    new_manifest = _release(
        tmp_path, "new", root_format="incompatible-next-format" if failure == "root" else "everos-1.2.3",
    )
    runtime, manager, processes = make(old_manifest)
    assert await runtime.wake() == {"ok": True, "state": "running"}
    old_python = manager.resolve_python()
    old_fingerprint = manager.artifact_fingerprint()
    root = home / "memory" / "everos-root"
    sentinel = root / "user-记忆.txt"
    sentinel.write_text("已有记忆保持不变", encoding="utf-8")
    before_root = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    before_config = asdict(config)
    await memory_runtime_factory.close(runtime)
    runtime, manager, processes = make(new_manifest)
    archive = new_manifest.parent / "memory-runtime-1.2.3-linux-arm64.tar.gz"
    if failure == "download":
        archive.unlink()
    elif failure == "digest":
        damaged = bytearray(archive.read_bytes())
        damaged[len(damaged) // 2] ^= 1
        archive.write_bytes(damaged)  # Same size, so only the SHA256 gate catches it.
    elif failure == "admission":
        monkeypatch.setattr(
            artifact_module, "run_cold_artifact_admission",
            lambda _binary: ColdArtifactAdmissionResult(
                ok=False, reason="memory_runtime_preparation_failed", duration_ms=1,
            ),
        )
    elif failure == "activation":
        class CandidateFailure(FakeEverOSProcess):
            async def start(self):
                if self.python != old_python:
                    return False
                return await super().start()
        processes.template = CandidateFailure

    result = await runtime.wake()

    assert result["state"] == "running"
    assert result["ok"] is True  # Availability, not successful upgrade.
    assert result["artifact_update"]["ok"] is False
    expected_reason = {
        "download": "memory-runtime_archive_download_failed",
        "digest": "memory-runtime_archive_checksum_mismatch",
        "admission": "memory_runtime_preparation_failed",
        "activation": "memory-runtime_install_failed",
        "root": "memory_local_data_unusable",
    }[failure]
    assert result["artifact_update"]["reason"] == expected_reason
    assert manager.status()["reason"]  # Failed update remains operator-visible.
    assert manager.status()["matches_manifest"] is False
    dependency = api._memory_runtime_row(
        api._MemoryRequirementProjection(True, "required"), manager.status(),
        action_class=api._memory_runtime_action_class(manager.status()),
    )
    assert dependency["has_update"] is True
    assert dependency["action_class"] == "repairable"
    assert dependency["reason"] == expected_reason
    assert manager.resolve_python() == old_python
    assert manager.artifact_fingerprint() == old_fingerprint
    assert sum(process.running for process in processes.created) == 1
    assert processes.supervised[-1].python == old_python
    assert runtime.needs_repair is False
    assert asdict(config) == before_config
    for name, content in before_root.items():
        assert (root / name).read_bytes() == content

    # Persisted failure must not turn a second failed attempt into an outage.
    again = await runtime.wake()
    assert again["ok"] is True
    assert again["artifact_update"]["ok"] is False
    assert manager.resolve_python() == old_python
