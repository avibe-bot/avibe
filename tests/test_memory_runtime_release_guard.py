from __future__ import annotations

import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
import zipfile

import pytest

from scripts import memory_runtime_release_guard as guard
from scripts.build_memory_runtime import LOCK_SHA256 as RUNTIME_LOCK_SHA256

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
    release_tag: str = "v3.1.0",
    requires_python: str = ">=3.10",
    supported_python_versions: tuple[str, ...] = ("3.10", "3.11", "3.12"),
) -> tuple[Path, dict[str, bytes]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tag = release_tag
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
        "package_policy": {
            "schema_version": 1,
            "release_tag": tag,
            "release_family": "3.1",
            "requires_python": requires_python,
            "supported_python_versions": list(supported_python_versions),
            "namespace_policy_version": 1,
        },
        "archives": archives,
    }
    manifest = tmp_path / "memory-runtime-manifest.json"
    manifest_bytes = (json.dumps(payload, sort_keys=True) + "\n").encode()
    manifest.write_bytes(manifest_bytes)
    remote[f"{base_url}/memory-runtime-manifest.json"] = manifest_bytes
    return manifest, remote


def _wheel(
    path: Path,
    name: str,
    *,
    version: str = "3.1.0",
    requires_python: str = ">=3.10",
    requires_dist: tuple[str, ...] = (),
    wheel_version: str = "1.0",
    include_wheel_metadata: bool = True,
    include_record: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    metadata = (
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
        f"Requires-Python: {requires_python}\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires_dist)
        + "\n"
    ).encode()
    files = {f"{dist_info}/METADATA": metadata}
    if include_wheel_metadata:
        files[f"{dist_info}/WHEEL"] = (
            f"Wheel-Version: {wheel_version}\nGenerator: gate5a-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n\n"
        ).encode()
    if include_record:
        record = f"{dist_info}/RECORD"
        files[record] = ("".join(f"{item},,\n" for item in sorted(files)) + f"{record},,\n").encode()
    with zipfile.ZipFile(path, "w") as archive:
        for member, content in files.items():
            archive.writestr(member, content)
    return path


def _transition_wheels(
    tmp_path: Path,
    *,
    version: str = "3.1.0",
    requires_python: str = ">=3.10",
    core_requirement: str = "avibe-memory==3.1.0",
    memory_requirement: str = "avibe-os>=3.1,<3.2",
) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    def build(name: str, requirement: str) -> Path:
        stem = name.replace("-", "_")
        return _wheel(tmp_path / f"{stem}-{version}-py3-none-any.whl", name, version=version, requires_python=requires_python, requires_dist=(requirement,))

    return build("avibe-os", core_requirement), build("avibe-memory", memory_requirement)


def _verify_static(tmp_path: Path, manifest: Path, **wheel_options):
    core, memory = _transition_wheels(tmp_path, **wheel_options)
    manifest_bytes = manifest.read_bytes()
    return guard.verify_static_transition(core, memory, release_tag=json.loads(manifest_bytes)["release_tag"], manifest_bytes=manifest_bytes, expected_manifest=manifest_bytes)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "schema_version", True),
        (None, "schema_version", 1.0),
        (None, "release_tag", "v9.9.9"),
        ("package_policy", "schema_version", True),
        ("package_policy", "schema_version", 1.0),
        ("package_policy", "release_tag", 1),
        ("package_policy", "supported_python_versions", "3.11"),
        ("package_policy", "namespace_policy_version", True),
    ],
)
def test_package_policy_requires_exact_json_types(
    tmp_path: Path, section: str | None, field: str, value: object
) -> None:
    manifest, _ = _manifest(tmp_path)
    payload = json.loads(manifest.read_bytes())
    target = payload if section is None else payload[section]
    target[field] = value
    candidate = json.dumps(payload).encode()

    with pytest.raises(guard.ManifestPolicyError):
        guard.load_package_release_policy(candidate, expected_manifest=candidate, release_tag="v3.1.0")
    if section is None and field == "schema_version":
        manifest.write_bytes(candidate)
        with pytest.raises(guard.ManifestPolicyError):
            guard.load_release_spec(manifest)


def test_manifest_byte_mismatch_precedes_invalid_semantic_policy(tmp_path: Path) -> None:
    selected, _ = _manifest(tmp_path)
    mismatched = b'{"schema_version":1,"package_policy":{"schema_version":2}}'

    with pytest.raises(guard.ReleaseAssetError, match="does not match"):
        guard.load_package_release_policy(mismatched, expected_manifest=selected.read_bytes(), release_tag="v3.1.0")


def test_wheel_filename_metadata_and_release_tag_are_independent_identities(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "avibe_os-3.1.1-py3-none-any.whl", "avibe-os")
    with pytest.raises(guard.ReleaseAssetError, match="filename identity"):
        guard.inspect_wheel(wheel)
    for directory, options in (("wheel", {"include_wheel_metadata": False}), ("record", {"include_record": False})):
        invalid = _wheel(tmp_path / directory / "avibe_os-3.1.0-py3-none-any.whl", "avibe-os", **options)
        with pytest.raises(guard.ReleaseAssetError, match="control structure"):
            guard.inspect_wheel(invalid)

    manifest, _ = _manifest(tmp_path / "release", release_tag="v3.1.1")
    with pytest.raises(guard.ReleaseAssetError, match="release tag"):
        _verify_static(tmp_path / "wheels", manifest)


def test_wheel_version_requires_complete_major_minor_syntax(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "avibe_os-3.1.0-py3-none-any.whl", "avibe-os", wheel_version="1.foo")
    with pytest.raises(guard.ReleaseAssetError, match="metadata version"):
        guard.inspect_wheel(wheel)


def test_static_transition_uses_release_bound_requires_python(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    core, memory, policy = _verify_static(tmp_path / "valid", manifest)
    assert core.version == memory.version == "3.1.0"
    assert policy.requires_python == ">=3.10"

    with pytest.raises(guard.ReleaseAssetError, match="Requires-Python"):
        _verify_static(tmp_path / "mismatch", manifest, requires_python=">=3.11")


def test_requirement_classification_keeps_wildcard_equality_non_exact(tmp_path: Path) -> None:
    classification = guard.classify_requirement("avibe-memory==3.1.*")
    assert classification.exact_version is None

    manifest, _ = _manifest(tmp_path)
    with pytest.raises(guard.ReleaseAssetError, match="hard-depend"):
        _verify_static(tmp_path / "wildcard", manifest, core_requirement="avibe-memory==3.1.*")
    with pytest.raises(guard.ReleaseAssetError, match="accept the release version"):
        _verify_static(tmp_path / "reverse", manifest, memory_requirement="avibe-os>=4")


def test_existing_guard_commands_remain_stdlib_only(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts/memory_runtime_release_guard.py"
    result = subprocess.run([sys._base_executable, "-S", str(script), "--manifest", str(manifest), "check-policy"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


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
