from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts import git_runtime_release_guard as guard


def _git_archive(binary: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as bundle:
            member = tarfile.TarInfo("bin/git")
            member.mode = 0o755
            member.size = len(binary)
            bundle.addfile(member, io.BytesIO(binary))
    return output.getvalue()


def _manifest(tmp_path: Path, archives: dict[str, bytes]) -> tuple[Path, dict[str, bytes]]:
    release_tag = "git-runtime-v2.55.0-1"
    base_url = f"{guard.RELEASE_DOWNLOAD_ROOT}/{release_tag}"
    remote: dict[str, bytes] = {}
    archive_payload = {}
    for platform, archive_bytes in archives.items():
        name = f"git-2.55.0-{platform}.tar.gz"
        archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as bundle:
            binary = bundle.extractfile("bin/git")
            assert binary is not None
            binary_sha256 = hashlib.sha256(binary.read()).hexdigest()
        url = f"{base_url}/{name}"
        archive_payload[platform] = {
            "name": name,
            "url": url,
            "sha256": archive_sha256,
            "binary_sha256": binary_sha256,
            "size": len(archive_bytes),
            "bin_path": "bin/git",
        }
        remote[url] = archive_bytes
        remote[f"{url}.sha256"] = f"{archive_sha256}  {name}\n".encode()

    payload = {
        "schema_version": 1,
        "git_version": "2.55.0",
        "release_tag": release_tag,
        "release_state": "published",
        "archives": archive_payload,
    }
    manifest_path = tmp_path / "git_runtime_manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    remote[f"{base_url}/git-runtime-manifest.json"] = manifest_path.read_bytes()
    return manifest_path, remote


def _fake_download(remote: dict[str, bytes]):
    def download(url: str, destination: Path, **_kwargs) -> None:
        try:
            payload = remote[url]
        except KeyError as exc:
            raise guard.ReleaseGuardError(f"missing test asset: {url}") from exc
        destination.write_bytes(payload)

    return download


def test_fetch_materializes_and_verifies_exact_release_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, remote = _manifest(
        tmp_path,
        {
            "linux-arm64": _git_archive(b"arm64-git"),
            "linux-x64": _git_archive(b"x64-git"),
        },
    )
    monkeypatch.setattr(guard, "_download_to_path", _fake_download(remote))

    spec = guard.fetch_release_assets(manifest_path, tmp_path / "backup")
    verified = guard.verify_release_assets(manifest_path, tmp_path / "backup")

    assert spec.release_tag == "git-runtime-v2.55.0-1"
    assert verified.expected_asset_names == {path.name for path in (tmp_path / "backup").iterdir()}


def test_verify_rejects_archive_checksum_mismatch(tmp_path: Path) -> None:
    manifest_path, remote = _manifest(tmp_path, {"linux-arm64": _git_archive(b"arm64-git")})
    backup = tmp_path / "backup"
    backup.mkdir()
    for url, payload in remote.items():
        (backup / url.rsplit("/", 1)[-1]).write_bytes(payload)
    archive = next(backup.glob("*.tar.gz"))
    archive.write_bytes(archive.read_bytes() + b"tampered")

    with pytest.raises(guard.ReleaseGuardError, match="size mismatch"):
        guard.verify_release_assets(manifest_path, backup)


def test_verify_rejects_unexpected_backup_asset(tmp_path: Path) -> None:
    manifest_path, remote = _manifest(tmp_path, {"linux-arm64": _git_archive(b"arm64-git")})
    backup = tmp_path / "backup"
    backup.mkdir()
    for url, payload in remote.items():
        (backup / url.rsplit("/", 1)[-1]).write_bytes(payload)
    (backup / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(guard.ReleaseGuardError, match="unexpected=.*unexpected.txt"):
        guard.verify_release_assets(manifest_path, backup)


def test_verify_rejects_non_file_backup_entry(tmp_path: Path) -> None:
    manifest_path, remote = _manifest(tmp_path, {"linux-arm64": _git_archive(b"arm64-git")})
    backup = tmp_path / "backup"
    backup.mkdir()
    for url, payload in remote.items():
        (backup / url.rsplit("/", 1)[-1]).write_bytes(payload)
    (backup / "nested").mkdir()

    with pytest.raises(guard.ReleaseGuardError, match="unsafe entries:.*nested"):
        guard.verify_release_assets(manifest_path, backup)


def test_failed_fetch_preserves_last_verified_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, remote = _manifest(tmp_path, {"linux-arm64": _git_archive(b"arm64-git")})
    del remote[next(url for url in remote if url.endswith(".tar.gz"))]
    monkeypatch.setattr(guard, "_download_to_path", _fake_download(remote))
    backup = tmp_path / "backup"
    backup.mkdir()
    marker = backup / "last-good"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(guard.ReleaseGuardError, match="missing test asset"):
        guard.fetch_release_assets(manifest_path, backup)

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_manifest_rejects_archive_url_outside_pinned_release(tmp_path: Path) -> None:
    manifest_path, _remote = _manifest(tmp_path, {"linux-arm64": _git_archive(b"arm64-git")})
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["archives"]["linux-arm64"]["url"] = "https://example.test/git.tar.gz"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(guard.ReleaseGuardError, match="outside the pinned release"):
        guard.load_release_spec(manifest_path)


def test_workflow_has_scheduled_backup_and_recovery_path() -> None:
    workflow = (guard.REPO_ROOT / ".github/workflows/git-runtime-release-guard.yml").read_text(
        encoding="utf-8"
    )

    assert "schedule:" in workflow
    assert "continue-on-error: true" in workflow
    assert "gh run download" in workflow
    assert "git-runtime-release-backup-${{ hashFiles('vibe/git_runtime_manifest.json') }}" in workflow
    assert "retention-days: 90" in workflow
    assert "--verify-tag" in workflow
    assert "--latest=false" in workflow
