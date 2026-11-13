"""Published sidecar consumption, without downloading or executing native code."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from core import managed_runtime
from core.direct_transport_runtime import DirectTransportRuntimeManager


# Independent publication-contract vectors, not imported from the consumer.
PLATFORMS = {
    "darwin-arm64": "aarch64-apple-darwin",
    "darwin-x64": "x86_64-apple-darwin",
    "linux-arm64": "aarch64-unknown-linux-musl",
    "linux-x64": "x86_64-unknown-linux-musl",
    "win32-x64": "x86_64-pc-windows-msvc",
}


@pytest.fixture
def published(tmp_path, monkeypatch):
    """Substitute only package/network inputs; retain real install/verify logic."""
    package = tmp_path / "package"
    package.mkdir()
    manifest_path = package / "direct_transport_runtime_manifest.json"
    tag = "direct-transport-v0.1.0-1"
    release_root = "https://github.com/avibe-bot/avibe/releases"
    payload = {
        "schema_version": 1,
        "direct_transport_version": "0.1.0",
        "release_state": "published",
        "source": "avibe-bot/avibe",
        "source_url": f"{release_root}/tag/{tag}",
        "release_tag": tag,
        "archives": {},
    }
    sources = {}
    for platform, target in PLATFORMS.items():
        name = f"avibe-direct-transport-{target}.tar.gz"
        executable = "avibe-direct-transport.exe" if platform == "win32-x64" else "avibe-direct-transport"
        binary = f"INERT test fixture, not a native executable: {target}\n".encode()
        archive = tmp_path / name
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo(executable)
            info.mode, info.size = 0o755, len(binary)
            tar.addfile(info, io.BytesIO(binary))
        url = f"{release_root}/download/{tag}/{name}"
        payload["archives"][platform] = {
            "name": name, "url": url, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "binary_sha256": hashlib.sha256(binary).hexdigest(), "size": archive.stat().st_size,
            "bin_path": executable, "format": "tar.gz",
        }
        sources[url] = archive
    manifest_path.write_text(json.dumps(payload))
    original_files = managed_runtime.package_resources.files
    monkeypatch.setattr(managed_runtime.package_resources, "files", lambda name: package if name == "vibe" else original_files(name))
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "linux-x64")
    downloads = []

    def fetch(url, path, **kwargs):
        downloads.append(url)
        shutil.copyfile(sources[url], path)

    monkeypatch.setattr(managed_runtime, "fetch_to_path", fetch)
    def forbidden_run(*args, **kwargs):
        pytest.fail("Installation/resolution must not execute the sidecar")
    monkeypatch.setattr(subprocess, "Popen", forbidden_run)
    return payload, manifest_path, downloads, sources


@pytest.mark.parametrize("platform", [*PLATFORMS, "win32-arm64"])
def test_install_selects_exact_platform_without_execution(tmp_path, monkeypatch, published, platform):
    payload, _, downloads, _ = published
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: platform)
    selected = "win32-x64" if platform == "win32-arm64" else platform
    archive = payload["archives"][selected]
    manager = DirectTransportRuntimeManager(runtime_dir=tmp_path / "runtime")
    result = manager.ensure()
    assert result["ok"] is True and result["changed"] is True
    assert result["platform"] == selected
    assert downloads == [archive["url"]]
    binary = manager.resolve_binary()
    assert binary == Path(result["path"])
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == archive["binary_sha256"]
    assert result["target"]["archive_sha256"] == archive["sha256"]
    assert result["target"]["binary_sha256"] == archive["binary_sha256"]
    status = manager.status()
    assert status["installed"] is True and status["matches_manifest"] is True
    assert manager.ensure()["changed"] is False
    assert downloads == [archive["url"]]
    binary.write_bytes(b"tampered fixture")
    assert manager.resolve_binary() is None
    assert manager.status()["installed"] is False
    assert manager.ensure()["ok"] is True
    assert manager.resolve_binary().read_bytes() != b"tampered fixture"


@pytest.mark.parametrize("change", [
    {"release_state": "pending"},
    {"release_state": None},
    {"release_state": "candidate"},
    {"schema_version": True},
    {"schema_version": 2},
    {"source": "somewhere/else"},
    {"release_tag": "latest"},
    {"source_url": "https://github.com/avibe-bot/avibe/releases/latest"},
    {"direct_transport_version": "0.2.0"},
    {"archives": {}},
])
def test_unqualified_pin_has_no_install_side_effects(tmp_path, published, change):
    payload, manifest_path, downloads, _ = published
    payload.update(change)
    manifest_path.write_text(json.dumps(payload))
    runtime = tmp_path / "runtime"
    manager = DirectTransportRuntimeManager(runtime_dir=runtime)
    assert manager.ensure()["ok"] is False
    assert manager.resolve_binary() is None
    assert manager.status()["installed"] is False
    assert downloads == [] and not runtime.exists()


@pytest.mark.parametrize("change", [
    {"url": "file:///tmp/sidecar.tar.gz"},
    {"url": "https://example.invalid/sidecar.tar.gz"},
    {"size": None}, {"size": True}, {"size": 0},
    {"bin_path": "../../sidecar"}, {"format": "binary"},
    {"binary_sha256": None}, {"sha256": "not-a-digest"},
])
def test_bad_archive_contract_refuses_before_download(tmp_path, published, change):
    payload, manifest_path, downloads, _ = published
    payload["archives"]["linux-x64"].update(change)
    manifest_path.write_text(json.dumps(payload))
    manager = DirectTransportRuntimeManager(runtime_dir=tmp_path / "runtime")
    assert manager.ensure()["reason"] == "direct-transport_manifest_invalid"
    assert downloads == [] and not manager.runtime_dir.exists()


@pytest.mark.parametrize("mutation", ["pending", "missing", "malformed", "new-pin", "no-selection"])
def test_no_previous_binary_fallback_after_pin_or_selection_change(tmp_path, published, mutation):
    payload, manifest_path, downloads, _ = published
    manager = DirectTransportRuntimeManager(runtime_dir=tmp_path / "runtime")
    assert manager.ensure()["ok"] is True
    before = list(downloads)
    if mutation == "pending":
        payload.update(release_state="pending", archives={})
        manifest_path.write_text(json.dumps(payload))
    elif mutation == "missing":
        manifest_path.unlink()
    elif mutation == "malformed":
        manifest_path.write_text("[")
    elif mutation == "new-pin":
        newer = copy.deepcopy(payload)
        newer["release_tag"] = "direct-transport-v0.1.0-2"
        newer["source_url"] = newer["source_url"].replace("0.1.0-1", "0.1.0-2")
        for archive in newer["archives"].values():
            archive["url"] = archive["url"].replace("0.1.0-1", "0.1.0-2")
        manifest_path.write_text(json.dumps(newer))
    else:
        (manager.runtime_dir / "current.json").unlink()
    assert manager.resolve_binary() is None
    assert manager.status()["installed"] is False
    assert downloads == before


@pytest.mark.parametrize("field,value,reason", [
    ("size", 1, "direct-transport_archive_size_mismatch"),
    ("sha256", "0" * 64, "direct-transport_archive_checksum_mismatch"),
    ("binary_sha256", "0" * 64, "direct-transport_binary_checksum_mismatch"),
])
def test_downloaded_bytes_must_match_pin(tmp_path, published, field, value, reason):
    payload, manifest_path, _, _ = published
    payload["archives"]["linux-x64"][field] = value
    manifest_path.write_text(json.dumps(payload))
    manager = DirectTransportRuntimeManager(runtime_dir=tmp_path / "runtime")
    result = manager.ensure()
    assert result["ok"] is False and result["reason"] == reason
    assert not (manager.runtime_dir / "current.json").exists()
    assert manager.resolve_binary() is None


def test_offline_and_unsupported_never_fetch(tmp_path, monkeypatch, published):
    _, _, downloads, _ = published
    manager = DirectTransportRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    assert manager.ensure()["reason"] == "direct-transport_archive_unavailable_offline"
    assert downloads == []
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "unsupported-platform")
    unsupported = DirectTransportRuntimeManager(runtime_dir=tmp_path / "unsupported")
    assert unsupported.ensure()["reason"] == "direct-transport_platform_unsupported"
    assert not unsupported.runtime_dir.exists() and downloads == []


def test_shipped_pending_manifest_ignores_environment_and_leaves_home_alone(tmp_path, monkeypatch):
    for name in ("VIBE_DIRECT_TRANSPORT_MANIFEST_PATH", "VIBE_DIRECT_TRANSPORT_MANIFEST_URL", "VIBE_DIRECT_TRANSPORT_BIN"):
        monkeypatch.setenv(name, str(tmp_path / "untrusted"))
    manager = DirectTransportRuntimeManager(runtime_dir=tmp_path / "runtime")
    assert manager.ensure()["reason"] == "direct-transport_runtime_unpublished"
    assert manager.resolve_binary() is None
    assert manager.status()["reason"] == "direct-transport_runtime_unpublished"
    assert not manager.runtime_dir.exists()
