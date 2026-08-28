from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
from pathlib import Path
import zipfile

import pytest

from scripts import memory_runtime_release_guard as guard
from scripts.build_memory_runtime import LOCK_SHA256 as RUNTIME_LOCK_SHA256

PYTHON = Path(sys.executable)
PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"


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
    supported_python_versions: tuple[str, ...] = (PYTHON_VERSION,),
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
BUILD_BACKEND = b"""from email.parser import Parser
from pathlib import Path
import zipfile
def get_requires_for_build_wheel(config_settings=None): return []
def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    metadata=Path("PKG-INFO").read_bytes(); message=Parser().parsestr(metadata.decode()); name=message["Name"].replace("-", "_"); version=message["Version"]
    dist_info, wheel_name = f"{name}-{version}.dist-info", f"{name}-{version}-py3-none-any.whl"
    files={p.as_posix():p.read_bytes() for p in Path().rglob("*") if p.is_file() and p.name not in {"PKG-INFO","pyproject.toml","_backend.py"} and "__pycache__" not in p.parts}
    files[f"{dist_info}/METADATA"] = metadata; files[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n\\n"
    record=f"{dist_info}/RECORD"; files[record]=("".join(f"{path},,\\n" for path in sorted(files))+f"{record},,\\n").encode()
    with zipfile.ZipFile(Path(wheel_directory) / wheel_name, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return wheel_name
"""


def _package(
    path: Path,
    name: str,
    requirements: tuple[str, ...] = (),
    files: dict[str, bytes] | None = None,
    *,
    version: str = VERSION,
    requires_python: str = ">=3.10",
) -> Path:
    metadata = (
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nRequires-Python: {requires_python}\n"
        + "".join(f"Requires-Dist: {item}\n" for item in requirements)
        + "\n"
    ).encode()
    members = dict(files or {})
    if path.suffix == ".whl":
        dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
        members[f"{dist_info}/METADATA"] = metadata
        members[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
        record = f"{dist_info}/RECORD"
        members[record] = ("".join(f"{item},,\n" for item in sorted(members)) + f"{record},,\n").encode()
        with zipfile.ZipFile(path, "w") as archive:
            for member_name, content in members.items():
                archive.writestr(member_name, content)
    else:
        members = {
            "PKG-INFO": metadata,
            "pyproject.toml": b'[build-system]\nrequires = []\nbuild-backend = "_backend"\nbackend-path = ["."]\n',
            "_backend.py": BUILD_BACKEND,
            **members,
        }
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


def _verify(package_dir: Path, rebuild_dir: Path) -> str:
    return guard.verify_transition_distributions(
        package_dir,
        rebuild_dir,
        release_tag="v3.1.0",
        python_executable=PYTHON,
    )


def _reject(root: Path, manifest: Path, error: str, **package_options) -> None:
    package_dir, _ = _transition_packages(root, manifest, **package_options)
    with pytest.raises(guard.ReleaseAssetError, match=error):
        _verify(package_dir, root / "rebuild")


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
    assert guard.discover_release_manifest(package_dir, python_executable=PYTHON) == guard.ManifestDiscovery(
        "memory", manifest.read_bytes()
    )
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    _package(
        legacy_dir / "avibe_os-3.0.13-py3-none-any.whl",
        "avibe-os",
        files={guard.MEMORY_MANIFEST_PATH: manifest.read_bytes()},
        version="3.0.13",
    )
    assert guard.discover_release_manifest(legacy_dir, python_executable=PYTHON).owner == "core"
    _package(next(legacy_dir.iterdir()), "avibe-os", files=CORE_FILES, version="3.0.13")
    with pytest.raises(guard.LegacyManifestAbsent):
        guard.discover_release_manifest(legacy_dir, python_executable=PYTHON, release_tag="gh-v3.0.14rc2")
    with pytest.raises(guard.ReleaseAssetError, match="transition-and-later"):
        guard.discover_release_manifest(legacy_dir, python_executable=PYTHON, release_tag="gh-v3.0.14rc3")
    artifacts["memory_wheel"].unlink()
    with pytest.raises(guard.ReleaseAssetError, match="transition avibe-memory artifact is missing"):
        guard.discover_release_manifest(package_dir, python_executable=PYTHON)


@pytest.mark.parametrize(
    ("package_options", "error"),
    [
        ({"memory_extra_files": {"core/controller.py": b"shadow\n"}}, "forbidden path policy"),
        ({"memory_extra_files": {f"avibe_memory-{VERSION}.data/scripts/vibe": b"shadow\n"}}, "forbidden path policy"),
        (
            {"core_files": {**CORE_FILES, "avibe_os-3.1.0.data/purelib/avibe_memory/runtime.py": b"shadow\n"}},
            "forbidden path policy",
        ),
        ({"core_files": {**CORE_FILES, guard.MEMORY_MANIFEST_PATH: b"owned\n"}}, "forbidden path policy"),
    ],
)
def test_package_guard_enforces_memory_namespace_policy_in_both_artifact_forms(
    tmp_path: Path, package_options: dict, error: str
) -> None:
    manifest, _ = _manifest(tmp_path)
    _reject(tmp_path, manifest, error, **package_options)


def test_package_guard_uses_manifest_frozen_python_policy(tmp_path: Path) -> None:
    manifest, _ = _manifest(
        tmp_path,
        package_requires_python=">=3.11",
        supported_python_versions=(PYTHON_VERSION,),
    )
    package_dir, _ = _transition_packages(tmp_path, manifest, requires_python=">=3.11")
    assert _verify(package_dir, tmp_path / "rebuild") == _verify(package_dir, tmp_path / "rebuild") == VERSION
    with pytest.raises(guard.ReleaseAssetError, match="interpreter is unavailable"):
        guard._run_interpreter(tmp_path / "missing-python", [], "probe")


def test_package_guard_rejects_manifest_byte_mismatch_before_policy_parsing(tmp_path: Path) -> None:
    selected, _ = _manifest(tmp_path)
    payload = json.loads(selected.read_bytes())
    payload["package_policy"]["schema_version"] = 2
    embedded = tmp_path / "embedded.json"
    embedded.write_text(json.dumps(payload))
    package_dir, _ = _transition_packages(tmp_path / "release", embedded)
    with pytest.raises(guard.ReleaseAssetError, match="does not match the selected manifest"):
        guard.verify_transition_distributions(
            package_dir,
            tmp_path / "rebuild",
            release_tag="v3.1.0",
            python_executable=PYTHON,
            expected_manifest=selected.read_bytes(),
        )


def test_sdist_rebuild_delegates_the_archive_directly_to_isolated_pip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _manifest(tmp_path)
    _, artifacts = _transition_packages(tmp_path, manifest)

    def run(_python: Path, argv: list[str], _failure: str):
        assert argv[0] == "wheel"
        assert argv[-1] == str(artifacts["core_sdist"].resolve())
        wheel_dir = Path(argv[argv.index("--wheel-dir") + 1])
        shutil.copyfile(artifacts["core_wheel"], wheel_dir / artifacts["core_wheel"].name)

    monkeypatch.setattr(guard, "_run_pip", run)
    rebuilt = guard.rebuild_sdist_wheel(
        artifacts["core_sdist"],
        tmp_path / "isolated",
        python_executable=PYTHON,
        find_links=(artifacts["core_sdist"].parent,),
    )
    assert rebuilt.name == artifacts["core_wheel"].name


def test_memory_indep_023_package_guard_rejects_dependency_and_metadata_drift(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    metadata = guard.PackageMetadata("avibe-os", VERSION, ">=3.10", ())
    for filename in ("avibe_os-3.1.1-py3-none-any.whl", "avibe_os-3.1.1.tar.gz"):
        with pytest.raises(guard.ReleaseAssetError, match="filename identity"):
            guard._assert_filename_identity(Path(filename), metadata)
    _reject(tmp_path / "python", manifest, "must match release policy >=3.10", requires_python=">=3.9")
    _reject(
        tmp_path / "wildcard",
        manifest,
        "hard-depend on the exact Memory version",
        core_requirements=("avibe-memory==3.1.*",),
    )

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
        _verify(package_dir, tmp_path / "parity-rebuild")


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
