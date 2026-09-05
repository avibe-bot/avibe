from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts import model_hub_engine_release_guard as guard


def _archive(binary: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as bundle:
            member = tarfile.TarInfo("cli-proxy-api")
            member.mode = 0o755
            member.size = len(binary)
            bundle.addfile(member, io.BytesIO(binary))
    return output.getvalue()


def _manifest(tmp_path: Path) -> tuple[Path, dict[str, bytes], dict[str, bytes]]:
    version = "v7.2.149"
    asset_release_tag = "model-hub-engine-v7.2.149-1"
    owned_root = f"{guard.OWNED_RELEASE_ROOT}/{asset_release_tag}"
    upstream_root = f"{guard.UPSTREAM_RELEASE_ROOT}/{version}"
    source_sha = "2a6b87aca083a5bf498ac1f68a1b636c500d7aaa"
    owned: dict[str, bytes] = {}
    upstream: dict[str, bytes] = {}
    assets = []
    for platform in sorted(guard.EXPECTED_PLATFORMS):
        upstream_platform = guard.UPSTREAM_PLATFORM_NAMES[platform]
        name = f"CLIProxyAPI_7.2.149_{upstream_platform}.tar.gz"
        archive = _archive(f"{platform}-binary".encode())
        archive_sha = hashlib.sha256(archive).hexdigest()
        binary_sha = hashlib.sha256(f"{platform}-binary".encode()).hexdigest()
        url = f"{owned_root}/{name}"
        assets.append(
            {
                "platform": platform,
                "url": url,
                "size_bytes": len(archive),
                "sha256": archive_sha,
                "binary_sha256": binary_sha,
                "bin_path": "cli-proxy-api",
            }
        )
        owned[url] = archive
        owned[f"{url}.sha256"] = f"{archive_sha}  {name}\n".encode()
        upstream[f"{upstream_root}/{name}"] = archive

    payload = {
        "schema_version": 1,
        "name": "cliproxyapi",
        "version": version,
        "source": "router-for-me/CLIProxyAPI",
        "source_url": f"https://github.com/router-for-me/CLIProxyAPI/tree/{source_sha}",
        "source_sha": source_sha,
        "release_tag": version,
        "asset_release_tag": asset_release_tag,
        "license": "MIT",
        "assets": assets,
    }
    manifest_path = tmp_path / "cliproxyapi_manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    owned[f"{owned_root}/model-hub-engine-manifest.json"] = manifest_path.read_bytes()
    return manifest_path, owned, upstream


def _fake_download(remote: dict[str, bytes]):
    def download(url: str, destination: Path, **_kwargs) -> None:
        try:
            payload = remote[url]
        except KeyError as exc:
            raise guard.ReleaseGuardError(f"missing test asset: {url}") from exc
        destination.write_bytes(payload)

    return download


def test_fetch_owned_release_materializes_exact_verified_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, owned, _upstream = _manifest(tmp_path)
    monkeypatch.setattr(guard, "_download", _fake_download(owned))

    spec = guard.fetch_release_assets(manifest_path, tmp_path / "backup")
    verified = guard.verify_release_assets(manifest_path, tmp_path / "backup")

    assert spec.asset_release_tag == "model-hub-engine-v7.2.149-1"
    assert verified.expected_asset_names == {
        path.name for path in (tmp_path / "backup").iterdir()
    }


def test_fetch_source_verifies_upstream_bytes_and_builds_publishable_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _owned, upstream = _manifest(tmp_path)
    monkeypatch.setattr(guard, "_download", _fake_download(upstream))

    spec = guard.fetch_upstream_assets(manifest_path, tmp_path / "publish")

    assert spec.upstream_release_tag == "v7.2.149"
    guard.verify_release_assets(manifest_path, tmp_path / "publish")


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda path: path.write_bytes(path.read_bytes() + b"tampered"), "size mismatch"),
        (lambda path: path.write_text("wrong checksum\n", encoding="utf-8"), "sidecar mismatch"),
    ],
)
def test_verify_rejects_changed_release_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    match,
) -> None:
    manifest_path, owned, _upstream = _manifest(tmp_path)
    monkeypatch.setattr(guard, "_download", _fake_download(owned))
    backup = tmp_path / "backup"
    guard.fetch_release_assets(manifest_path, backup)
    target = next(backup.glob("*.tar.gz" if match == "size mismatch" else "*.sha256"))
    mutate(target)

    with pytest.raises(guard.ReleaseGuardError, match=match):
        guard.verify_release_assets(manifest_path, backup)


@pytest.mark.parametrize("entry_kind", ["unexpected", "directory"])
def test_verify_rejects_non_release_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    manifest_path, owned, _upstream = _manifest(tmp_path)
    monkeypatch.setattr(guard, "_download", _fake_download(owned))
    backup = tmp_path / "backup"
    guard.fetch_release_assets(manifest_path, backup)
    if entry_kind == "unexpected":
        (backup / "unexpected.txt").write_text("unexpected", encoding="utf-8")
        expected = "unexpected=.*unexpected.txt"
    else:
        (backup / "nested").mkdir()
        expected = "unsafe entries"

    with pytest.raises(guard.ReleaseGuardError, match=expected):
        guard.verify_release_assets(manifest_path, backup)


def test_failed_fetch_preserves_last_verified_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, owned, _upstream = _manifest(tmp_path)
    del owned[next(url for url in owned if url.endswith(".tar.gz"))]
    monkeypatch.setattr(guard, "_download", _fake_download(owned))
    backup = tmp_path / "backup"
    backup.mkdir()
    marker = backup / "last-good"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(guard.ReleaseGuardError, match="missing test asset"):
        guard.fetch_release_assets(manifest_path, backup)

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_manifest_rejects_non_owned_release_url(tmp_path: Path) -> None:
    manifest_path, _owned, _upstream = _manifest(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["assets"][0]["url"] = (
        "https://github.com/router-for-me/CLIProxyAPI/releases/download/"
        "v7.2.149/CLIProxyAPI_7.2.149_darwin_aarch64.tar.gz"
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(guard.ReleaseGuardError, match="outside the Avibe-owned release"):
        guard.load_release_spec(manifest_path)


def test_workflow_has_scheduled_backup_and_non_clobbering_recovery() -> None:
    workflow = (
        guard.REPO_ROOT / ".github/workflows/model-hub-engine-release-guard.yml"
    ).read_text(encoding="utf-8")

    assert "schedule:" in workflow
    assert "continue-on-error: true" in workflow
    assert 'actions/artifacts"' in workflow
    assert "MANIFEST_SHA: ${{ steps.manifest.outputs.sha256 }}" in workflow
    assert "model-hub-engine-release-backup-${{ steps.manifest.outputs.sha256 }}" in workflow
    assert "retention-days: 90" in workflow
    assert "--verify-tag" in workflow
    assert "--latest=false" in workflow
    assert "missing_assets" in workflow
    assert "--clobber" not in workflow
