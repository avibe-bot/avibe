"""Hermetic coverage for the release-only historical Runtime guard."""

import hashlib
import json
from pathlib import Path
import tarfile

import pytest

from scripts import memory_runtime_release_guard as guard


def _fixture(tmp_path: Path, *, tag="v3.1.0", malformed=False):
    payload = {
        "schema_version": 1, "release_state": "published", "release_tag": tag,
        "everos_version": "1.2.3", "python_version": guard.EXPECTED_PYTHON_VERSION,
        "lock_sha256": guard.EXPECTED_LOCK_SHA256,
        "lock_id": f"uv-lock-sha256:{guard.EXPECTED_LOCK_SHA256}",
        "uv_version": guard.EXPECTED_UV_VERSION,
        "archives": {},
    }
    archive = tmp_path / "runtime-linux-x64.tgz"
    binary = b"historical runtime binary"
    member = tarfile.TarInfo("bin/runtime")
    member.size = len(binary)
    with tarfile.open(archive, "w:gz") as bundle:
        stream = __import__("io").BytesIO(binary)
        bundle.addfile(member, stream)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    payload["archives"] = {
        platform: {"name": name, "url": f"https://github.com/avibe-bot/avibe/releases/download/{tag}/{name}",
                   "sha256": digest, "binary_sha256": hashlib.sha256(binary).hexdigest(),
                   "size": archive.stat().st_size, "bin_path": "bin/runtime"}
        for platform, name in (("darwin-arm64", "runtime-darwin-arm64.tgz"),
                               ("linux-arm64", "runtime-linux-arm64.tgz"),
                               ("linux-x64", "runtime-linux-x64.tgz"))
    }
    manifest = tmp_path / "memory-runtime-manifest.json"
    manifest.write_text(json.dumps(payload))
    if malformed:
        manifest.write_text("{}")
    return manifest, payload, archive


def test_pinned_manifest_and_asset_hash_are_strict(tmp_path):
    manifest, payload, archive = _fixture(tmp_path)
    spec = guard.load_release_spec(manifest)
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    (asset_dir / manifest.name).write_bytes(manifest.read_bytes())
    for item in spec.archives:
        (asset_dir / item.name).write_bytes(archive.read_bytes())
    assert guard.verify_release_assets(manifest, asset_dir).release_tag == "v3.1.0"
    (asset_dir / "runtime-linux-x64.tgz").write_bytes(b"tampered")
    with pytest.raises(guard.ReleaseAssetError):
        guard.verify_release_assets(manifest, asset_dir)
    payload["release_tag"] = "v9.9.9"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(guard.ManifestPolicyError):
        guard.load_release_spec(manifest)


def test_malformed_historical_manifest_is_excluded_not_reconstructed(tmp_path):
    manifest, _, _ = _fixture(tmp_path, malformed=True)
    with pytest.raises(guard.ManifestPolicyError):
        guard.load_release_spec(manifest)


def test_fetch_uses_pinned_urls_and_refuses_existing_output(monkeypatch, tmp_path):
    manifest, _, archive = _fixture(tmp_path)
    calls = []

    def download(url, destination, expected_size, attempts=3):
        calls.append(url)
        if url.endswith("manifest.json"):
            destination.write_bytes(manifest.read_bytes())
        else:
            destination.write_bytes(archive.read_bytes())

    monkeypatch.setattr(guard, "_download", download)
    output = tmp_path / "out"
    guard.fetch_release_assets(manifest, output)
    assert calls == [
        "https://github.com/avibe-bot/avibe/releases/download/v3.1.0/memory-runtime-manifest.json",
        "https://github.com/avibe-bot/avibe/releases/download/v3.1.0/runtime-darwin-arm64.tgz",
        "https://github.com/avibe-bot/avibe/releases/download/v3.1.0/runtime-linux-arm64.tgz",
        "https://github.com/avibe-bot/avibe/releases/download/v3.1.0/runtime-linux-x64.tgz",
    ]
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    guard.fetch_release_assets(manifest, output)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before
