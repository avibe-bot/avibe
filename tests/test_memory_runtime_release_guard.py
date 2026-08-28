from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
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


def _package_metadata(name: str, version: str, requirements: tuple[str, ...]) -> bytes:
    fields = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        "Requires-Python: >=3.10",
        *(f"Requires-Dist: {requirement}" for requirement in requirements),
    ]
    return ("\n".join(fields) + "\n\n").encode()


def _wheel(
    path: Path,
    *,
    name: str,
    version: str,
    requirements: tuple[str, ...],
    files: dict[str, bytes],
) -> Path:
    dist_info = name.replace("-", "_") + f"-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        for member_name, content in {
            **files,
            f"{dist_info}/METADATA": _package_metadata(name, version, requirements),
        }.items():
            archive.writestr(member_name, content)
    return path


def _sdist(
    path: Path,
    *,
    name: str,
    version: str,
    requirements: tuple[str, ...],
    files: dict[str, bytes],
) -> Path:
    root = f"{name.replace('-', '_')}-{version}"
    with tarfile.open(path, "w:gz") as archive:
        for member_name, content in {
            "PKG-INFO": _package_metadata(name, version, requirements),
            **files,
        }.items():
            member = tarfile.TarInfo(f"{root}/{member_name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return path


def _transition_packages(tmp_path: Path, manifest: Path) -> tuple[Path, dict[str, Path]]:
    package_dir = tmp_path / "packages"
    package_dir.mkdir(parents=True)
    version = "3.1.0"
    core_files = {"core/memory_loader.py": b"MEMORY_ENTRYPOINT = 'avibe_memory.runtime'\n"}
    memory_files = {
        "avibe_memory/__init__.py": b"from .runtime import start\n",
        "avibe_memory/runtime.py": b"def start(): return None\n",
        guard.MEMORY_MANIFEST_PATH: manifest.read_bytes(),
    }
    artifacts = {
        "core_wheel": _wheel(
            package_dir / f"avibe_os-{version}-py3-none-any.whl",
            name="avibe-os",
            version=version,
            requirements=(f"avibe-memory=={version}",),
            files=core_files,
        ),
        "core_sdist": _sdist(
            package_dir / f"avibe_os-{version}.tar.gz",
            name="avibe-os",
            version=version,
            requirements=(f"avibe-memory=={version}",),
            files=core_files,
        ),
        "memory_wheel": _wheel(
            package_dir / f"avibe_memory-{version}-py3-none-any.whl",
            name="avibe-memory",
            version=version,
            requirements=("avibe-os>=3.0.14.dev0,<3.2",),
            files=memory_files,
        ),
        "memory_sdist": _sdist(
            package_dir / f"avibe_memory-{version}.tar.gz",
            name="avibe-memory",
            version=version,
            requirements=("avibe-os>=3.0.14.dev0,<3.2",),
            files=memory_files,
        ),
    }
    return package_dir, artifacts


def _copy_builder(artifacts: dict[str, Path], built: list[str]):
    def build(sdist: Path, output_dir: Path) -> Path:
        kind = "memory" if sdist.name.startswith("avibe_memory-") else "core"
        built.append(kind)
        output_dir.mkdir(parents=True)
        source = artifacts[f"{kind}_wheel"]
        target = output_dir / source.name
        shutil.copyfile(source, target)
        return target

    return build


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


def test_memory_indep_022_dual_form_discovery_reads_legacy_core_and_transition_memory(
    tmp_path: Path,
) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest)

    transition = guard.discover_release_manifest(package_dir)
    assert transition == guard.ManifestDiscovery("memory", manifest.read_bytes())

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    _wheel(
        legacy_dir / "avibe_os-3.0.13-py3-none-any.whl",
        name="avibe-os",
        version="3.0.13",
        requirements=(),
        files={guard.MEMORY_MANIFEST_PATH: manifest.read_bytes()},
    )
    legacy = guard.discover_release_manifest(legacy_dir)
    assert legacy == guard.ManifestDiscovery("core", manifest.read_bytes())

    artifacts["memory_wheel"].unlink()
    with pytest.raises(guard.ReleaseAssetError, match="transition avibe-memory artifact is missing"):
        guard.discover_release_manifest(package_dir)


def test_memory_indep_022_023_discovery_distinguishes_legacy_and_ambiguous_transition(
    tmp_path: Path,
) -> None:
    manifest, _ = _manifest(tmp_path)
    legacy_dir = tmp_path / "manifest-free"
    legacy_dir.mkdir()
    _wheel(
        legacy_dir / "avibe_os-3.0.8-py3-none-any.whl",
        name="avibe-os",
        version="3.0.8",
        requirements=(),
        files={"core/memory_loader.py": b"legacy\n"},
    )
    with pytest.raises(guard.LegacyManifestAbsent):
        guard.discover_release_manifest(legacy_dir, release_tag="gh-v3.0.14rc2")
    with pytest.raises(guard.ReleaseAssetError, match="transition-and-later"):
        guard.discover_release_manifest(legacy_dir, release_tag="v3.0.14")

    package_dir, artifacts = _transition_packages(tmp_path / "ambiguous", manifest)
    _wheel(
        artifacts["core_wheel"],
        name="avibe-os",
        version="3.1.0",
        requirements=("avibe-memory==3.1.0",),
        files={
            "core/memory_loader.py": b"loader\n",
            guard.MEMORY_MANIFEST_PATH: manifest.read_bytes(),
        },
    )
    with pytest.raises(guard.ReleaseAssetError, match="ownership is ambiguous"):
        guard.discover_release_manifest(package_dir)


def test_memory_indep_023_package_guard_reuses_assertions_for_both_rebuilt_wheels(
    tmp_path: Path,
) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest)
    built: list[str] = []

    version = guard.verify_transition_distributions(
        package_dir,
        tmp_path / "rebuild",
        release_tag="v3.1.0",
        builder=_copy_builder(artifacts, built),
    )

    assert version == "3.1.0"
    assert built == ["core", "memory"]


def test_default_sdist_builder_uses_pep517_isolation_without_importing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = _manifest(tmp_path)
    _, artifacts = _transition_packages(tmp_path, manifest)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs):
        calls.append(argv)
        assert "--no-isolation" not in argv
        assert kwargs["env"]["PYTHONPATH"] == ""
        project_root = Path(argv[-1])
        assert (project_root / "PKG-INFO").is_file()
        wheel_dir = Path(argv[argv.index("--outdir") + 1])
        shutil.copyfile(artifacts["core_wheel"], wheel_dir / artifacts["core_wheel"].name)
        return guard.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(guard.subprocess, "run", run)
    rebuilt = guard.rebuild_sdist_wheel(artifacts["core_sdist"], tmp_path / "isolated")

    assert rebuilt.name == artifacts["core_wheel"].name
    assert calls[0][1:4] == ["-m", "build", "--wheel"]


def test_transition_package_guard_rejects_staged_and_rebuilt_ownership_drift(
    tmp_path: Path,
) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest)
    _sdist(
        artifacts["core_sdist"],
        name="avibe-os",
        version="3.1.0",
        requirements=("avibe-memory==3.1.0",),
        files={
            "core/memory_loader.py": b"loader\n",
            guard.MEMORY_MANIFEST_PATH: manifest.read_bytes(),
        },
    )
    with pytest.raises(guard.ReleaseAssetError, match="core artifact owns Memory content"):
        guard.verify_transition_distributions(
            package_dir,
            tmp_path / "staged-rebuild",
            release_tag="v3.1.0",
            builder=_copy_builder(artifacts, []),
        )

    package_dir, artifacts = _transition_packages(tmp_path / "rebuilt", manifest)
    bad_core = _wheel(
        tmp_path / "bad-core.whl",
        name="avibe-os",
        version="3.1.0",
        requirements=("avibe-memory==3.1.0",),
        files={guard.MEMORY_MANIFEST_PATH: manifest.read_bytes()},
    )

    def bad_builder(sdist: Path, output_dir: Path) -> Path:
        if sdist.name.startswith("avibe_os-"):
            return bad_core
        return _copy_builder(artifacts, [])(sdist, output_dir)

    with pytest.raises(guard.ReleaseAssetError, match="core artifact owns Memory content"):
        guard.verify_transition_distributions(
            package_dir,
            tmp_path / "bad-rebuild",
            release_tag="v3.1.0",
            builder=bad_builder,
        )


def test_memory_indep_023_package_guard_rejects_dependency_and_metadata_drift(
    tmp_path: Path,
) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest)
    _wheel(
        artifacts["core_wheel"],
        name="avibe-os",
        version="3.1.0",
        requirements=("avibe-memory==3.1.1",),
        files={"core/memory_loader.py": b"MEMORY_ENTRYPOINT = 'avibe_memory.runtime'\n"},
    )
    _sdist(
        artifacts["core_sdist"],
        name="avibe-os",
        version="3.1.0",
        requirements=("avibe-memory==3.1.1",),
        files={"core/memory_loader.py": b"MEMORY_ENTRYPOINT = 'avibe_memory.runtime'\n"},
    )

    with pytest.raises(guard.ReleaseAssetError, match="hard-depend on the exact Memory version"):
        guard.verify_transition_distributions(
            package_dir,
            tmp_path / "metadata-rebuild",
            release_tag="v3.1.0",
            builder=_copy_builder(artifacts, []),
        )

    package_dir, artifacts = _transition_packages(tmp_path / "metadata", manifest)
    _sdist(
        artifacts["memory_sdist"],
        name="avibe-memory",
        version="3.1.0",
        requirements=("avibe-os>=3.1,<3.2",),
        files={
            "avibe_memory/__init__.py": b"from .runtime import start\n",
            "avibe_memory/runtime.py": b"def start(): return None\n",
            guard.MEMORY_MANIFEST_PATH: manifest.read_bytes(),
        },
    )
    with pytest.raises(guard.ReleaseAssetError, match="wheel and sdist metadata differ"):
        guard.verify_transition_distributions(
            package_dir,
            tmp_path / "parity-rebuild",
            release_tag="v3.1.0",
            builder=_copy_builder(artifacts, []),
        )

    package_dir, artifacts = _transition_packages(tmp_path / "reverse", manifest)
    for key in ("memory_wheel", "memory_sdist"):
        factory = _wheel if key.endswith("wheel") else _sdist
        factory(
            artifacts[key],
            name="avibe-memory",
            version="3.1.0",
            requirements=("avibe-os>=4",),
            files={
                "avibe_memory/__init__.py": b"from .runtime import start\n",
                "avibe_memory/runtime.py": b"def start(): return None\n",
                guard.MEMORY_MANIFEST_PATH: manifest.read_bytes(),
            },
        )
    with pytest.raises(guard.ReleaseAssetError, match="must accept the exact core version"):
        guard.verify_transition_distributions(
            package_dir,
            tmp_path / "reverse-rebuild",
            release_tag="v3.1.0",
            builder=_copy_builder(artifacts, []),
        )


def test_transition_package_guard_binds_distribution_version_to_release_tag(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest)

    with pytest.raises(guard.ReleaseAssetError, match="does not match the release tag"):
        guard.verify_transition_distributions(
            package_dir,
            tmp_path / "tag-rebuild",
            release_tag="v3.1.1",
            builder=_copy_builder(artifacts, []),
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


def test_memory_indep_022_023_public_verifier_redownloads_every_manifest_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, remote = _manifest(tmp_path)
    downloaded: list[str] = []
    fake_download = _fake_download(remote)

    def tracked_download(url: str, destination: Path, expected_size: int, attempts: int = 3) -> None:
        downloaded.append(url)
        fake_download(url, destination, expected_size, attempts)

    monkeypatch.setattr(guard, "_download", tracked_download)
    spec = guard.verify_public_release_assets(manifest)

    assert spec.release_tag == "v3.1.0"
    assert set(downloaded) == set(remote)

    archive_url = next(url for url in remote if url.endswith(".tar.gz"))
    remote[archive_url] = b"x" * len(remote[archive_url])
    with pytest.raises(guard.ReleaseAssetError, match="integrity mismatch"):
        guard.verify_public_release_assets(manifest)


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
    assert "discover-manifest" in resolution
    assert 'elif [ "$discovery_status" -eq 2 ]' in resolution
    assert "avibe_memory-*.whl" in resolution
    assert "fromJSON(needs.resolve_manifests.outputs.manifests)" in workflow
    assert "matrix.manifest.release_tag" in workflow
    assert "matrix.manifest.owner == 'memory'" in workflow
    assert "verify-packages --asset-dir" in workflow
    assert workflow.count("python3 -m pip install packaging") == 2
    assert "python3 -m pip install build" in workflow
