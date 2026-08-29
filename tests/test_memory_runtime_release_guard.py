from __future__ import annotations

import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from scripts import memory_runtime_release_guard as guard
from scripts.build_memory_runtime import LOCK_SHA256 as RUNTIME_LOCK_SHA256

ROOT = Path(__file__).resolve().parents[1]


def test_guard_platform_contract_keeps_no_follow_capable_shipped_targets_enabled() -> None:
    assert guard.EXPECTED_PLATFORMS == frozenset({"darwin-arm64", "linux-arm64", "linux-x64"})


def test_guard_lock_hash_matches_canonical_runtime_lock() -> None:
    lockfile = Path(__file__).resolve().parents[1] / "scripts/memory_runtime/uv.lock"

    assert guard.EXPECTED_LOCK_SHA256 == RUNTIME_LOCK_SHA256
    assert guard.EXPECTED_LOCK_SHA256 == hashlib.sha256(lockfile.read_bytes()).hexdigest()


def _archive(binary: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as bundle:
            member = tarfile.TarInfo("bin/python")
            member.mode = 0o755
            member.size = len(binary)
            bundle.addfile(member, io.BytesIO(binary))
    return output.getvalue()


def _manifest(
    tmp_path: Path,
    *,
    everos_version: str = "1.2.3",
    lock_sha256: str = guard.EXPECTED_LOCK_SHA256,
) -> tuple[Path, dict[str, bytes]]:
    tag = "v3.1.0"
    base_url = f"{guard.RELEASE_DOWNLOAD_ROOT}/{tag}"
    archives: dict[str, dict[str, object]] = {}
    remote: dict[str, bytes] = {}
    for platform in sorted(guard.EXPECTED_PLATFORMS):
        binary = f"python-{platform}".encode()
        archive = _archive(binary)
        name = f"memory-runtime-{everos_version}-{platform}.tar.gz"
        url = f"{base_url}/{name}"
        archives[platform] = {
            "name": name,
            "url": url,
            "sha256": hashlib.sha256(archive).hexdigest(),
            "binary_sha256": hashlib.sha256(binary).hexdigest(),
            "size": len(archive),
            "bin_path": "bin/python",
        }
        remote[url] = archive
    payload = {
        "schema_version": 1,
        "everos_version": everos_version,
        "python_version": guard.EXPECTED_PYTHON_VERSION,
        "lock_sha256": lock_sha256,
        "lock_id": f"uv-lock-sha256:{lock_sha256}",
        "uv_version": guard.EXPECTED_UV_VERSION,
        "release_state": "published",
        "release_tag": tag,
        "archives": archives,
    }
    manifest = tmp_path / "memory-runtime-manifest.json"
    manifest_bytes = (json.dumps(payload, sort_keys=True) + "\n").encode()
    manifest.write_bytes(manifest_bytes)
    remote[f"{base_url}/memory-runtime-manifest.json"] = manifest_bytes
    return manifest, remote


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_guard_policy_requires_integer_schema_version(
    tmp_path: Path,
    schema_version: object,
) -> None:
    manifest, _ = _manifest(tmp_path)
    payload = json.loads(manifest.read_bytes())
    payload["schema_version"] = schema_version
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(guard.ManifestPolicyError, match="schema_version"):
        guard.load_release_spec(manifest)


def test_existing_guard_commands_remain_stdlib_only(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    script = ROOT / "scripts/memory_runtime_release_guard.py"

    result = subprocess.run(
        [
            sys._base_executable,
            "-S",
            str(script),
            "--manifest",
            str(manifest),
            "check-policy",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_retired_gate5_verifier_symbols_have_no_production_or_workflow_callers() -> None:
    retired_symbols = (
        "Package" + "Metadata",
        "PackageRelease" + "Policy",
        "Requirement" + "Classification",
        "load_package_release_" + "policy",
        "classify_" + "requirement",
        "inspect_" + "wheel",
        "verify_static_" + "transition",
        "verify_wheel_" + "transition",
    )
    sources = [
        *sorted((ROOT / "scripts").rglob("*.py")),
        *sorted((ROOT / ".github/workflows").glob("*.yml")),
        *sorted((ROOT / ".github/workflows").glob("*.yaml")),
    ]

    references = {
        symbol: path.relative_to(ROOT).as_posix()
        for path in sources
        for symbol in retired_symbols
        if symbol in path.read_text(encoding="utf-8")
    }

    assert references == {}


@pytest.mark.parametrize("everos_version", sorted(guard.PUBLISHED_RUNTIME_PROVENANCE))
def test_guard_accepts_every_published_runtime_provenance(
    tmp_path: Path,
    everos_version: str,
) -> None:
    provenance = guard.PUBLISHED_RUNTIME_PROVENANCE[everos_version]
    manifest, _ = _manifest(
        tmp_path,
        everos_version=everos_version,
        lock_sha256=provenance.lock_sha256,
    )

    spec = guard.load_release_spec(manifest)

    assert spec.release_tag == "v3.1.0"


def test_guard_keeps_gh_v3_0_9rc3_runtime_in_coverage() -> None:
    assert guard.PUBLISHED_RUNTIME_PROVENANCE["1.1.3"] == guard.RuntimeProvenance(
        python_version="3.12.12",
        lock_sha256="62b00f1a9ca04cc4ea4c5af51f389ba49acdea8786e5f7044d52823244502c57",
        uv_version="0.9.18",
    )


def test_guard_rejects_unknown_runtime_provenance(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path, everos_version="1.2.2")

    with pytest.raises(guard.ManifestPolicyError, match="published supported EverOS version"):
        guard.load_release_spec(manifest)


def test_guard_cli_distinguishes_policy_rejection_from_missing_bytes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unsupported, _ = _manifest(tmp_path, everos_version="1.2.2")

    policy_status = guard.main(["--manifest", str(unsupported), "check-policy"])
    policy_result = json.loads(capsys.readouterr().err)

    assert policy_status == guard.POLICY_EXCLUSION_EXIT
    assert policy_result["failure_kind"] == "policy"

    supported, _ = _manifest(tmp_path)
    bytes_status = guard.main(
        [
            "--manifest",
            str(supported),
            "verify",
            "--asset-dir",
            str(tmp_path / "missing-assets"),
        ]
    )
    bytes_result = json.loads(capsys.readouterr().err)

    assert bytes_status == guard.ASSET_FAILURE_EXIT
    assert bytes_result["failure_kind"] == "bytes"


def _fake_download(remote: dict[str, bytes]):
    def download(url: str, destination: Path, expected_size: int, attempts: int = 3) -> None:
        del attempts
        try:
            payload = remote[url]
        except KeyError as exc:
            raise guard.ReleaseAssetError(f"missing test asset: {url}") from exc
        assert len(payload) == expected_size
        destination.write_bytes(payload)

    return download


def test_current_workflow_guard_command_shapes_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, remote = _manifest(tmp_path)
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    for url, payload in remote.items():
        (asset_dir / url.rsplit("/", 1)[-1]).write_bytes(payload)
    monkeypatch.setattr(guard, "_download", _fake_download(remote))

    command_lines = (
        ["--manifest", str(manifest), "check-policy"],
        ["--manifest", str(manifest), "verify", "--asset-dir", str(asset_dir)],
        [
            "--manifest",
            str(manifest),
            "fetch",
            "--output-dir",
            str(tmp_path / "fetched"),
        ],
    )

    for argv in command_lines:
        assert guard.main(argv) == 0
        result = json.loads(capsys.readouterr().out)
        assert result == {"ok": True, "release_tag": "v3.1.0", "asset_count": 4}


def test_fetch_and_verify_exact_memory_runtime_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, remote = _manifest(tmp_path)
    monkeypatch.setattr(guard, "_download", _fake_download(remote))

    spec = guard.fetch_release_assets(manifest, tmp_path / "backup")
    verified = guard.verify_release_assets(manifest, tmp_path / "backup")

    assert spec.release_tag == "v3.1.0"
    assert verified.expected_asset_names == {path.name for path in (tmp_path / "backup").iterdir()}


def test_verify_rejects_changed_archive(tmp_path: Path) -> None:
    manifest, remote = _manifest(tmp_path)
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "memory-runtime-manifest.json").write_bytes(manifest.read_bytes())
    for url, value in remote.items():
        (backup / url.rsplit("/", 1)[-1]).write_bytes(value)
    archive = next(backup.glob("*.tar.gz"))
    archive.write_bytes(archive.read_bytes() + b"changed")

    with pytest.raises(guard.ReleaseGuardError, match="integrity mismatch"):
        guard.verify_release_assets(manifest, backup)


def test_failed_fetch_preserves_last_verified_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, remote = _manifest(tmp_path)
    remote.pop(next(iter(remote)))
    monkeypatch.setattr(guard, "_download", _fake_download(remote))
    backup = tmp_path / "backup"
    backup.mkdir()
    marker = backup / "last-good"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(guard.ReleaseGuardError, match="missing test asset"):
        guard.fetch_release_assets(manifest, backup)

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_fetch_rejects_missing_published_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, remote = _manifest(tmp_path)
    spec = guard.load_release_spec(manifest)
    manifest_url = f"{guard.RELEASE_DOWNLOAD_ROOT}/{spec.release_tag}/memory-runtime-manifest.json"
    remote.pop(manifest_url)
    monkeypatch.setattr(guard, "_download", _fake_download(remote))

    with pytest.raises(guard.ReleaseGuardError, match="missing test asset"):
        guard.fetch_release_assets(manifest, tmp_path / "backup")


def test_fetch_rejects_changed_published_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, remote = _manifest(tmp_path)
    spec = guard.load_release_spec(manifest)
    manifest_url = f"{guard.RELEASE_DOWNLOAD_ROOT}/{spec.release_tag}/memory-runtime-manifest.json"
    remote[manifest_url] = remote[manifest_url].replace(b'"release_tag": "v3.1.0"', b'"release_tag": "v3.1.1"')
    monkeypatch.setattr(guard, "_download", _fake_download(remote))

    with pytest.raises(guard.ReleaseGuardError, match="published Memory Runtime manifest differs"):
        guard.fetch_release_assets(manifest, tmp_path / "backup")


def test_download_aborts_and_removes_partial_file_when_response_exceeds_manifest_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response(io.BytesIO):
        headers: dict[str, str] = {}

    monkeypatch.setattr(
        guard.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"too-large"),
    )
    destination = tmp_path / "archive.tar.gz"

    with pytest.raises(guard.ReleaseGuardError, match="exceeds manifest size"):
        guard._download("https://example.test/archive.tar.gz", destination, expected_size=3)

    assert not destination.exists()


def test_guard_workflow_has_scheduled_backup_and_non_clobbering_recovery() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/memory-runtime-release-guard.yml").read_text(
        encoding="utf-8"
    )

    assert "schedule:" in workflow
    assert "continue-on-error: true" not in workflow
    assert "steps.probe.outputs.result == 'bytes_failure'" in workflow
    assert "gh run download" in workflow
    assert "memory-runtime-release-backup-${{ matrix.manifest.sha256 }}" in workflow
    assert "retention-days: 90" in workflow
    assert "missing=(" in workflow
    assert "--clobber" not in workflow


def test_guard_workflow_reports_and_verifies_supported_published_manifests() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/memory-runtime-release-guard.yml").read_text(
        encoding="utf-8"
    )

    resolution = workflow.split("- name: Resolve every published Memory Runtime manifest", 1)[1]
    resolution = resolution.split("  guard:", 1)[0]
    assert "manifests=" in resolution
    assert "break" not in resolution
    assert "check-policy" in resolution
    assert "Guarded Memory Runtime manifests" in resolution
    assert "Excluded Memory Runtime manifests" in resolution
    assert "fromJSON(needs.resolve_manifests.outputs.manifests)" in workflow
    assert "matrix.manifest.release_tag" in workflow


def test_guard_workflow_reads_manifests_from_memory_distribution() -> None:
    workflow = (ROOT / ".github/workflows/memory-runtime-release-guard.yml").read_text(encoding="utf-8")

    assert workflow.count("--pattern 'avibe_memory-*.whl'") == 2
    assert workflow.count('glob("avibe_memory-*.whl")') == 2
    assert 'startsWith("avibe_memory-")' not in workflow
    assert 'startswith("avibe_memory-")' in workflow
    assert "avibe_os-*.whl" not in workflow
