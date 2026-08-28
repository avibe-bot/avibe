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
    package_requires_python: str = ">=3.10",
    supported_python_versions: tuple[str, ...] = ("3.10", "3.11", "3.12"),
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
        "package_policy": {
            "schema_version": 1,
            "release_tag": tag,
            "release_family": "3.1",
            "requires_python": package_requires_python,
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


VERSION = "3.1.0"
CORE_FILES = {"core/memory_loader.py": b"MEMORY_ENTRYPOINT = 'avibe_memory.runtime'\n"}


def _package(
    path: Path,
    name: str,
    requirements: tuple[str, ...] = (),
    files: dict[str, bytes] | None = None,
    *,
    version: str = VERSION,
    requires_python: str = ">=3.10",
    wheel_version: str | None = "1.0",
    dist_info_prefix: str = "",
) -> Path:
    metadata = "\n".join(
        [
            "Metadata-Version: 2.4",
            f"Name: {name}",
            f"Version: {version}",
            f"Requires-Python: {requires_python}",
            *(f"Requires-Dist: {item}" for item in requirements),
            "",
            "",
        ]
    ).encode()
    members = dict(files or {})
    if path.suffix == ".whl":
        dist_info = f"{dist_info_prefix}{name.replace('-', '_')}-{version}.dist-info"
        members |= {f"{dist_info}/METADATA": metadata, f"{dist_info}/RECORD": b""}
        if wheel_version is not None:
            members[f"{dist_info}/WHEEL"] = (
                f"Wheel-Version: {wheel_version}\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
            ).encode()
        with zipfile.ZipFile(path, "w") as archive:
            for member_name, content in members.items():
                archive.writestr(member_name, content)
    else:
        members = {"PKG-INFO": metadata, **members}
        with tarfile.open(path, "w:gz") as archive:
            for member_name, content in members.items():
                member = tarfile.TarInfo(f"{name.replace('-', '_')}-{version}/{member_name}")
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return path


def _transition_packages(
    root: Path,
    manifest: Path,
    *,
    requires_python: str = ">=3.10",
    core_files: dict[str, bytes] | None = None,
    core_requirements: tuple[str, ...] = (f"avibe-memory=={VERSION}",),
    memory_extra_files: dict[str, bytes] | None = None,
    memory_requirements: tuple[str, ...] = ("avibe-os>=3.0.14.dev0,<3.2",),
) -> tuple[Path, dict[str, Path]]:
    package_dir = root / "packages"
    package_dir.mkdir(parents=True)
    memory_files = {
        "avibe_memory/__init__.py": b"from .runtime import start\n",
        "avibe_memory/runtime.py": b"def start(): return None\n",
        guard.MEMORY_MANIFEST_PATH: manifest.read_bytes(),
        **(memory_extra_files or {}),
    }
    artifacts: dict[str, Path] = {}
    for key, name, requirements, files in (
        ("core", "avibe-os", core_requirements, core_files or CORE_FILES),
        ("memory", "avibe-memory", memory_requirements, memory_files),
    ):
        stem = name.replace("-", "_") + f"-{VERSION}"
        for form, suffix in (("wheel", "-py3-none-any.whl"), ("sdist", ".tar.gz")):
            artifacts[f"{key}_{form}"] = _package(
                package_dir / f"{stem}{suffix}",
                name,
                requirements,
                files,
                requires_python=requires_python,
            )
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


def _verify(package_dir: Path, rebuild_dir: Path, artifacts: dict[str, Path], tag: str = "v3.1.0") -> str:
    return guard.verify_transition_distributions(
        package_dir, rebuild_dir, release_tag=tag, builder=_copy_builder(artifacts, [])
    )


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


def test_memory_indep_022_dual_form_discovery_reads_legacy_core_and_transition_memory(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest)
    assert guard.discover_release_manifest(package_dir) == guard.ManifestDiscovery("memory", manifest.read_bytes())
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    _package(
        legacy_dir / "avibe_os-3.0.13-py3-none-any.whl",
        "avibe-os",
        files={guard.MEMORY_MANIFEST_PATH: manifest.read_bytes()},
        version="3.0.13",
    )
    assert guard.discover_release_manifest(legacy_dir).owner == "core"
    _package(next(legacy_dir.iterdir()), "avibe-os", files=CORE_FILES, version="3.0.13")
    with pytest.raises(guard.LegacyManifestAbsent):
        guard.discover_release_manifest(legacy_dir, release_tag="gh-v3.0.14rc2")
    with pytest.raises(guard.ReleaseAssetError, match="transition-and-later"):
        guard.discover_release_manifest(legacy_dir, release_tag="gh-v3.0.14rc3")
    artifacts["memory_wheel"].unlink()
    with pytest.raises(guard.ReleaseAssetError, match="transition avibe-memory artifact is missing"):
        guard.discover_release_manifest(package_dir)


@pytest.mark.parametrize(
    ("artifact_key", "wrong_name"),
    [("core_wheel", "avibe_os-3.1.1-py3-none-any.whl"), ("memory_sdist", "avibe_memory-3.1.1.tar.gz")],
)
def test_package_guard_binds_parsed_filenames_to_metadata_and_release(
    tmp_path: Path, artifact_key: str, wrong_name: str
) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest)
    artifacts[artifact_key] = artifacts[artifact_key].rename(package_dir / wrong_name)
    with pytest.raises(guard.ReleaseAssetError, match="filename identity"):
        _verify(package_dir, tmp_path / "rebuild", artifacts)


def test_package_guard_uses_offline_pip_for_complete_dependency_resolution(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(
        tmp_path,
        manifest,
        core_requirements=("avibe-memory==3.1.0", "shared==1"),
        memory_requirements=("avibe-os>=3.1,<3.2", "shared==2"),
    )
    for version in ("1", "2"):
        _package(
            package_dir / f"shared-{version}-py3-none-any.whl",
            "shared",
            files={"shared/__init__.py": b""},
            version=version,
        )
    with pytest.raises(guard.ReleaseAssetError, match="offline pip resolution"):
        _verify(package_dir, tmp_path / "rebuild", artifacts)


@pytest.mark.parametrize("forbidden_path", ["core/controller.py", "vibe/__init__.py"])
def test_package_guard_enforces_memory_namespace_policy_in_both_artifact_forms(
    tmp_path: Path, forbidden_path: str
) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest, memory_extra_files={forbidden_path: b"shadow\n"})
    with pytest.raises(guard.ReleaseAssetError, match="namespace policy"):
        _verify(package_dir, tmp_path / "rebuild", artifacts)


def test_package_guard_uses_manifest_frozen_python_policy(tmp_path: Path) -> None:
    manifest, _ = _manifest(
        tmp_path,
        package_requires_python=">=3.11",
        supported_python_versions=("3.11", "3.12"),
    )
    package_dir, artifacts = _transition_packages(tmp_path, manifest, requires_python=">=3.11")
    built: list[str] = []
    version = guard.verify_transition_distributions(
        package_dir, tmp_path / "rebuild", release_tag="v3.1.0", builder=_copy_builder(artifacts, built)
    )
    assert (version, built) == (VERSION, ["core", "memory"])


@pytest.mark.parametrize(
    ("wheel_version", "dist_info_prefix"),
    [(None, ""), ("2.0", ""), ("1.0", "nested/")],
)
def test_package_guard_uses_pip_wheel_structure_validation(
    tmp_path: Path, wheel_version: str | None, dist_info_prefix: str
) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest)
    _package(
        artifacts["core_wheel"],
        "avibe-os",
        (f"avibe-memory=={VERSION}",),
        CORE_FILES,
        wheel_version=wheel_version,
        dist_info_prefix=dist_info_prefix,
    )

    with pytest.raises(guard.ReleaseAssetError, match="wheel structure validation"):
        _verify(package_dir, tmp_path / "rebuild", artifacts)


def test_default_sdist_builder_uses_pep517_isolation_without_importing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _manifest(tmp_path)
    _, artifacts = _transition_packages(tmp_path, manifest)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs):
        calls.append(argv)
        assert "--no-isolation" not in argv
        assert kwargs["env"]["PYTHONPATH"] == ""
        assert (Path(argv[-1]) / "PKG-INFO").is_file()
        wheel_dir = Path(argv[argv.index("--outdir") + 1])
        shutil.copyfile(artifacts["core_wheel"], wheel_dir / artifacts["core_wheel"].name)
        return guard.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(guard.subprocess, "run", run)
    rebuilt = [guard.rebuild_sdist_wheel(artifacts["core_sdist"], tmp_path / "isolated") for _ in range(2)]
    assert {path.name for path in rebuilt} == {artifacts["core_wheel"].name}
    assert rebuilt[0].parent != rebuilt[1].parent
    assert len(calls) == 2
    assert calls[0][1:4] == ["-m", "build", "--wheel"]


@pytest.mark.parametrize("wheel_scheme", ["purelib", "platlib"])
def test_transition_package_guard_rejects_staged_and_rebuilt_ownership_drift(tmp_path: Path, wheel_scheme: str) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest)
    _package(
        artifacts["core_sdist"],
        "avibe-os",
        (f"avibe-memory=={VERSION}",),
        files={
            "core/memory_loader.py": b"loader\n",
            guard.MEMORY_MANIFEST_PATH: manifest.read_bytes(),
        },
    )
    with pytest.raises(guard.ReleaseAssetError, match="namespace policy"):
        _verify(package_dir, tmp_path / "staged-rebuild", artifacts)

    package_dir, artifacts = _transition_packages(tmp_path / "rebuilt", manifest)
    bad_core = _package(
        tmp_path / "avibe_os-3.1.0-py3-none-any.whl",
        "avibe-os",
        (f"avibe-memory=={VERSION}",),
        files={guard.MEMORY_MANIFEST_PATH: manifest.read_bytes()},
    )

    def bad_builder(sdist: Path, output_dir: Path) -> Path:
        if sdist.name.startswith("avibe_os-"):
            return bad_core
        return _copy_builder(artifacts, [])(sdist, output_dir)

    with pytest.raises(guard.ReleaseAssetError, match="namespace policy"):
        guard.verify_transition_distributions(
            package_dir, tmp_path / "bad-rebuild", release_tag="v3.1.0", builder=bad_builder
        )

    package_dir, artifacts = _transition_packages(
        tmp_path / wheel_scheme,
        manifest,
        core_files={
            "core/memory_loader.py": b"loader\n",
            f"avibe_os-3.1.0.data/{wheel_scheme}/avibe_memory/runtime.py": b"shadow\n",
        },
    )
    with pytest.raises(guard.ReleaseAssetError, match="namespace policy"):
        _verify(package_dir, tmp_path / f"{wheel_scheme}-rebuild", artifacts)


def test_memory_indep_023_package_guard_rejects_dependency_and_metadata_drift(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    package_dir, artifacts = _transition_packages(tmp_path, manifest, requires_python=">=99")
    with pytest.raises(guard.ReleaseAssetError, match="must match release policy >=3.10"):
        _verify(package_dir, tmp_path / "python-range-rebuild", artifacts)

    package_dir, artifacts = _transition_packages(
        tmp_path / "pin", manifest, core_requirements=("avibe-memory==3.1.1",)
    )
    with pytest.raises(guard.ReleaseAssetError, match="hard-depend on the exact Memory version"):
        _verify(package_dir, tmp_path / "metadata-rebuild", artifacts)

    package_dir, artifacts = _transition_packages(tmp_path / "metadata", manifest)
    _package(
        artifacts["memory_sdist"],
        "avibe-memory",
        ("avibe-os>=3.1,<3.2",),
        files={
            "avibe_memory/__init__.py": b"from .runtime import start\n",
            "avibe_memory/runtime.py": b"def start(): return None\n",
            guard.MEMORY_MANIFEST_PATH: manifest.read_bytes(),
        },
    )
    with pytest.raises(guard.ReleaseAssetError, match="wheel and sdist metadata differ"):
        _verify(package_dir, tmp_path / "parity-rebuild", artifacts)

    package_dir, artifacts = _transition_packages(tmp_path / "reverse", manifest, memory_requirements=("avibe-os>=4",))
    with pytest.raises(guard.ReleaseAssetError, match="must accept the exact core version"):
        _verify(package_dir, tmp_path / "reverse-rebuild", artifacts)

    package_dir, artifacts = _transition_packages(tmp_path / "tag", manifest)
    with pytest.raises(guard.ManifestPolicyError, match="package_policy identity"):
        _verify(package_dir, tmp_path / "tag-rebuild", artifacts, tag="v3.1.1")


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
    assert "fromJSON(needs.resolve_manifests.outputs.manifests)" in workflow
    assert "matrix.manifest.release_tag" in workflow
