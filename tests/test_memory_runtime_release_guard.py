"""Hermetic coverage for the release-only historical Runtime guard."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest

from scripts import memory_runtime_release_guard as guard


def _fixture(tmp_path: Path, *, tag="v3.1.0", malformed=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
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


def test_failed_fetch_preserves_previous_verified_backup_output(monkeypatch, tmp_path):
    manifest, _, archive = _fixture(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    (output / "verified.bin").write_bytes(b"verified backup")

    def fail_download(url, destination, expected_size, attempts=3):
        if url.endswith("manifest.json"):
            destination.write_bytes(manifest.read_bytes())
            return
        raise guard.ReleaseAssetError("fixture network failure")

    monkeypatch.setattr(guard, "_download", fail_download)
    with pytest.raises(guard.ReleaseAssetError):
        guard.fetch_release_assets(manifest, output)
    assert (output / "verified.bin").read_bytes() == b"verified backup"


def test_resolve_manifests_shell_selects_history_and_excludes_new_or_invalid_wheels(tmp_path):
    """Execute the workflow's resolver with a fake gh release API/download."""
    import yaml

    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/memory-runtime-release-guard.yml").read_text())
    step = next(
        step for step in workflow["jobs"]["resolve_manifests"]["steps"]
        if step.get("id") == "manifests"
    )
    root = tmp_path / "releases"
    root.mkdir()
    old_manifest, _, _ = _fixture(tmp_path / "old")

    def wheel(path: Path, name: str, manifest: bytes | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            if manifest is not None:
                archive.writestr("vibe/memory_runtime_manifest.json", manifest)
            archive.writestr(f"{name}.dist-info/METADATA", f"Name: {name}\nVersion: 3.2.0\n")
            archive.writestr(f"{name}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
            archive.writestr(f"{name}.dist-info/RECORD", "")

    wheel(root / "v3.1.0" / "avibe_os-3.1.0-py3-none-any.whl", "avibe_os", old_manifest.read_bytes())
    inert = tmp_path / "inert"
    subprocess.run([
        sys.executable, str(Path(__file__).parents[1] / "scripts/build_retired_companion.py"),
        "--tag", "gh-v3.2.0rc1", "--output-dir", str(inert),
    ], check=True, capture_output=True)
    (root / "gh-v3.2.0rc1").mkdir()
    shutil.copy2(next(inert.glob("*.whl")), root / "gh-v3.2.0rc1/avibe_memory-3.2.0rc1-py3-none-any.whl")
    wheel(root / "v3.2.0" / "avibe_memory-3.2.0-py3-none-any.whl", "avibe_memory")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_bin.joinpath("gh").write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, shutil, sys\n"
        "args=sys.argv[1:]\n"
        "if args[:2] == ['api', '--paginate']:\n"
        " print('v3.1.0\\tavibe-os\\tavibe_os-*.whl\\n' 'gh-v3.2.0rc1\\tavibe-memory\\tavibe_memory-*.whl\\n' 'v3.2.0\\tavibe-memory\\tavibe_memory-*.whl')\n"
        "elif args[:2] == ['release', 'download']:\n"
        " tag=args[2]; pattern=args[args.index('--pattern')+1]; destination=pathlib.Path(args[args.index('--dir')+1]); destination.mkdir(parents=True, exist_ok=True)\n"
        f" source=pathlib.Path({str(root)!r})/tag\n"
        " matches=list(source.glob(pattern)); assert len(matches)==1, (tag,pattern)\n"
        " shutil.copy2(matches[0], destination/matches[0].name)\n"
        "else: raise SystemExit(args)\n",
        encoding="utf-8",
    )
    fake_bin.joinpath("gh").chmod(0o755)
    output = tmp_path / "github-output"
    summary = tmp_path / "summary"
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    env = {
        **os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "fixture/repo",
        "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary), "RUNNER_TEMP": str(runner_temp),
    }
    release_bytes_before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
                            cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    records = json.loads(values["manifests"])
    excluded = json.loads(values["excluded"])
    assert values["available"] == "true"
    assert [item["release_tag"] for item in records] == ["v3.1.0"]
    reasons = {item["release_tag"]: item["reason"] for item in excluded}
    assert "verified inert compatibility bridge" in reasons["gh-v3.2.0rc1"]
    assert "not a verified inert bridge" in reasons["v3.2.0"]
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == release_bytes_before
