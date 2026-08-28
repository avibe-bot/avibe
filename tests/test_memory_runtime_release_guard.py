from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import struct
import subprocess
import sys
import tarfile
from pathlib import Path
import zipfile

import pytest
from packaging import tags as packaging_tags

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
    wheel_tag: object = "py3-none-any",
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
            "wheel_tag": wheel_tag,
            "namespace_policy_version": 1,
        },
        "archives": archives,
    }
    manifest = tmp_path / "memory-runtime-manifest.json"
    manifest_bytes = (json.dumps(payload, sort_keys=True) + "\n").encode()
    manifest.write_bytes(manifest_bytes)
    remote[f"{base_url}/memory-runtime-manifest.json"] = manifest_bytes
    return manifest, remote


def _package_metadata(
    name: str,
    *,
    version: str = "3.1.0",
    requires_python: str = ">=3.10",
    requires_dist: tuple[str, ...] = (),
) -> guard.PackageMetadata:
    return guard.PackageMetadata(name, version, requires_python, requires_dist)


def _verify_static(
    manifest: Path,
    *,
    metadata_version: str = "3.1.0",
    filename_version: str | None = None,
    requires_python: str = ">=3.10",
    core_requirement: str = "avibe-memory==3.1.0",
    memory_requirement: str = "avibe-os==3.1.0",
) -> tuple[guard.PackageMetadata, guard.PackageMetadata, guard.PackageReleasePolicy]:
    filename_version = filename_version or metadata_version
    core = _package_metadata(
        "avibe-os",
        version=metadata_version,
        requires_python=requires_python,
        requires_dist=(core_requirement,),
    )
    memory = _package_metadata(
        "avibe-memory",
        version=metadata_version,
        requires_python=requires_python,
        requires_dist=(memory_requirement,),
    )
    manifest_bytes = manifest.read_bytes()
    return guard.verify_static_transition(
        core_wheel_filename=f"avibe_os-{filename_version}-py3-none-any.whl",
        core_metadata=core,
        memory_wheel_filename=f"avibe_memory-{filename_version}-py3-none-any.whl",
        memory_metadata=memory,
        release_tag=json.loads(manifest_bytes)["release_tag"],
        manifest_bytes=manifest_bytes,
        expected_manifest=manifest_bytes,
    )


def _wheel(
    path: Path,
    name: str,
    *,
    metadata_name: str | None = None,
    metadata_distribution_version: str = "3.1.0",
    metadata_version: str | None = "2.4",
    requires_dist: tuple[str, ...] = (),
    dist_info: str | None = None,
    extra_dist_info: bool = False,
    include_metadata: bool = True,
    include_wheel: bool = True,
    wheel_version: str = "1.0",
    root_is_purelib: tuple[str, ...] = ("true",),
    wheel_tags: tuple[str, ...] = ("py3-none-any",),
    wheel_trailer: str = "\n",
    duplicate_control: str | None = None,
    include_record: bool = True,
    record_bytes: bytes | None = None,
    extra_files: dict[str, bytes] | None = None,
    compression: int = zipfile.ZIP_STORED,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    dist_info = dist_info or f"{name.replace('-', '_')}-3.1.0.dist-info"
    metadata_header = "" if metadata_version is None else f"Metadata-Version: {metadata_version}\n"
    metadata = (
        f"{metadata_header}Name: {metadata_name or name}\n"
        f"Version: {metadata_distribution_version}\nRequires-Python: >=3.10\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires_dist)
        + "\n"
    ).encode()
    wheel = (
        f"Wheel-Version: {wheel_version}\nGenerator: gate5a-test\n"
        + "".join(f"Root-Is-Purelib: {value}\n" for value in root_is_purelib)
        + "".join(f"Tag: {value}\n" for value in wheel_tags)
        + wheel_trailer
    ).encode()
    files = {f"{name.replace('-', '_')}/__init__.py": b"x = 1\n"}
    files.update(extra_files or {})
    if include_metadata:
        files[f"{dist_info}/METADATA"] = metadata
    if include_wheel:
        files[f"{dist_info}/WHEEL"] = wheel
    if extra_dist_info:
        files["other-3.1.0.dist-info/METADATA"] = metadata
    record = f"{dist_info}/RECORD"
    if include_record:
        if record_bytes is None:
            rows = []
            for member, content in files.items():
                digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
                rows.append(f"{member},sha256={digest},{len(content)}\n")
            rows.append(f"{record},,\n")
            record_bytes = "".join(rows).encode()
        files[record] = record_bytes
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for member, content in files.items():
            archive.writestr(member, content)
        if duplicate_control is not None:
            member = f"{dist_info}/{duplicate_control}"
            archive.writestr(member, files[member])
    return path


def _verify_wheels(
    tmp_path: Path,
    manifest: Path,
    *,
    filename_tag: str = "py3-none-any",
    core_options: dict[str, object] | None = None,
    memory_options: dict[str, object] | None = None,
) -> tuple[guard.PackageMetadata, guard.PackageMetadata, guard.PackageReleasePolicy]:
    core = _wheel(
        tmp_path / f"avibe_os-3.1.0-{filename_tag}.whl",
        "avibe-os",
        requires_dist=("avibe-memory==3.1.0",),
        **(core_options or {}),
    )
    memory = _wheel(
        tmp_path / f"avibe_memory-3.1.0-{filename_tag}.whl",
        "avibe-memory",
        requires_dist=("avibe-os==3.1.0",),
        **(memory_options or {}),
    )
    manifest_bytes = manifest.read_bytes()
    return guard.verify_wheel_transition(
        core,
        memory,
        release_tag=json.loads(manifest_bytes)["release_tag"],
        manifest_bytes=manifest_bytes,
        expected_manifest=manifest_bytes,
    )


def _make_first_zip_member_unreadable(wheel: Path, *, encrypted: bool) -> None:
    content = bytearray(wheel.read_bytes())
    local = content.index(b"PK\x03\x04")
    central = content.index(b"PK\x01\x02")
    if encrypted:
        for offset in (local + 6, central + 8):
            struct.pack_into("<H", content, offset, struct.unpack_from("<H", content, offset)[0] | 1)
    else:
        struct.pack_into("<H", content, local + 8, 99)
        struct.pack_into("<H", content, central + 10, 99)
    wheel.write_bytes(content)


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
        ("package_policy", "supported_python_versions", ["3.11rc1"]),
        ("package_policy", "supported_python_versions", ["3.11+local"]),
        ("package_policy", "supported_python_versions", ["3.11.post1"]),
        ("package_policy", "supported_python_versions", ["3.11.dev1"]),
        ("package_policy", "wheel_tag", True),
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


@pytest.mark.parametrize("requires_python", [">=3.10", ">= 3.10"])
def test_package_policy_accepts_declared_requires_python_literal(
    tmp_path: Path, requires_python: str
) -> None:
    manifest, _ = _manifest(tmp_path, requires_python=requires_python)
    manifest_bytes = manifest.read_bytes()

    policy = guard.load_package_release_policy(
        manifest_bytes, expected_manifest=manifest_bytes, release_tag="v3.1.0"
    )

    assert policy.requires_python == ">=3.10"
    assert policy.supported_python_versions == guard.PACKAGE_POLICY_SUPPORTED_PYTHON_VERSIONS
    assert policy.wheel_tag == "py3-none-any"


def test_package_policy_rejects_undeclared_wheel_tag(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path, wheel_tag="py3-none-manylinux_2_17_x86_64")
    manifest_bytes = manifest.read_bytes()

    with pytest.raises(guard.ManifestPolicyError, match="wheel_tag"):
        guard.load_package_release_policy(
            manifest_bytes, expected_manifest=manifest_bytes, release_tag="v3.1.0"
        )


@pytest.mark.parametrize(
    "supported_python_versions",
    [
        ("3.9",),
        ("3.10", "3.11"),
        ("3.10", "3.11", "3.12", "3.13"),
        ("3.10", "3.11", "3.12", "3.12"),
        ("3.12", "3.11", "3.10"),
    ],
)
def test_package_policy_rejects_undeclared_supported_python_versions(
    tmp_path: Path, supported_python_versions: tuple[str, ...]
) -> None:
    manifest, _ = _manifest(tmp_path, supported_python_versions=supported_python_versions)
    manifest_bytes = manifest.read_bytes()

    with pytest.raises(guard.ManifestPolicyError, match="supported Python"):
        guard.load_package_release_policy(
            manifest_bytes, expected_manifest=manifest_bytes, release_tag="v3.1.0"
        )


@pytest.mark.parametrize("requires_python", ["==3.10", ">=3.10,<3.10.2", ">=3.10,!=3.10.1"])
def test_package_policy_rejects_undeclared_requires_python_contract(
    tmp_path: Path, requires_python: str
) -> None:
    manifest, _ = _manifest(tmp_path, requires_python=requires_python)
    manifest_bytes = manifest.read_bytes()

    with pytest.raises(guard.ManifestPolicyError, match="Requires-Python"):
        guard.load_package_release_policy(
            manifest_bytes, expected_manifest=manifest_bytes, release_tag="v3.1.0"
        )


def test_wheel_filename_metadata_and_release_tag_are_independent_identities(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path / "metadata")
    with pytest.raises(guard.ReleaseAssetError, match="filename identity"):
        _verify_static(manifest, filename_version="3.1.1")

    manifest, _ = _manifest(tmp_path / "release", release_tag="v3.1.1")
    with pytest.raises(guard.ReleaseAssetError, match="release tag"):
        _verify_static(manifest)


def test_static_transition_uses_release_bound_requires_python(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    core, memory, policy = _verify_static(manifest)
    assert core.version == memory.version == "3.1.0"
    assert policy.requires_python == ">=3.10"

    with pytest.raises(guard.ReleaseAssetError, match="Requires-Python"):
        _verify_static(manifest, requires_python=">=3.11")


def test_requirement_classification_keeps_wildcard_equality_non_exact(tmp_path: Path) -> None:
    classification = guard.classify_requirement("avibe-memory==3.1.*")
    assert classification.exact_version is None

    manifest, _ = _manifest(tmp_path)
    with pytest.raises(guard.ReleaseAssetError, match="hard-depend"):
        _verify_static(manifest, core_requirement="avibe-memory==3.1.*")
    with pytest.raises(guard.ReleaseAssetError, match="exact avibe-os"):
        _verify_static(manifest, memory_requirement="avibe-os>=4")
    with pytest.raises(guard.ReleaseAssetError, match="exact avibe-os"):
        _verify_static(manifest, memory_requirement="avibe-os>=0")


def test_wheel_transition_checks_manifest_identity_before_opening_wheels(tmp_path: Path) -> None:
    """MEMORY-INDEP-022: manifest identity precedes wheel parsing."""
    manifest, _ = _manifest(tmp_path)

    with pytest.raises(guard.ReleaseAssetError, match="does not match"):
        guard.verify_wheel_transition(
            tmp_path / "missing-core.whl",
            tmp_path / "missing-memory.whl",
            release_tag="v3.1.0",
            manifest_bytes=b'{"unsupported_semantics":true}',
            expected_manifest=manifest.read_bytes(),
        )


def test_wheel_transition_accepts_declared_control_metadata(tmp_path: Path) -> None:
    """MEMORY-INDEP-023: wheel controls bind to declared release policy."""
    manifest, _ = _manifest(tmp_path)

    core, memory, policy = _verify_wheels(tmp_path / "wheels", manifest)

    assert (core.name, memory.name) == ("avibe-os", "avibe-memory")
    assert policy.wheel_tag == "py3-none-any"


@pytest.mark.parametrize(
    "core_options",
    [
        {"metadata_name": "other"},
        {"metadata_distribution_version": "3.1.1"},
        {"dist_info": "other-3.1.0.dist-info"},
        {"extra_dist_info": True},
    ],
)
def test_wheel_binds_filename_metadata_and_top_level_dist_info(
    tmp_path: Path, core_options: dict[str, object]
) -> None:
    manifest, _ = _manifest(tmp_path)

    with pytest.raises(guard.ReleaseAssetError, match="identity|control structure"):
        _verify_wheels(tmp_path / "wheels", manifest, core_options=core_options)


@pytest.mark.parametrize("missing", ["metadata", "wheel"])
def test_wheel_requires_control_files(tmp_path: Path, missing: str) -> None:
    manifest, _ = _manifest(tmp_path)
    options = {f"include_{missing}": False}

    with pytest.raises(guard.ReleaseAssetError, match="control policy"):
        _verify_wheels(tmp_path / "wheels", manifest, core_options=options)


@pytest.mark.parametrize("control", ["METADATA", "WHEEL"])
def test_wheel_rejects_duplicate_control_files(tmp_path: Path, control: str) -> None:
    manifest, _ = _manifest(tmp_path)

    with pytest.warns(UserWarning, match="Duplicate name"):
        with pytest.raises(guard.ReleaseAssetError, match="control|archive structure"):
            _verify_wheels(
                tmp_path / control,
                manifest,
                core_options={"duplicate_control": control},
            )


@pytest.mark.parametrize(
    ("wheel_version", "accepted"),
    [("1.0", True), ("1.1", True), ("1", False), ("1.foo", False), ("2.0", False)],
)
def test_wheel_version_is_complete_and_supported(
    tmp_path: Path, wheel_version: str, accepted: bool
) -> None:
    manifest, _ = _manifest(tmp_path)
    verify = lambda: _verify_wheels(
        tmp_path / wheel_version.replace(".", "-"),
        manifest,
        core_options={"wheel_version": wheel_version},
    )

    if accepted:
        verify()
    else:
        with pytest.raises(guard.ReleaseAssetError, match="control policy"):
            verify()


@pytest.mark.parametrize("values", [(), ("false",), ("true", "true")])
def test_wheel_requires_exactly_one_purelib_declaration(
    tmp_path: Path, values: tuple[str, ...]
) -> None:
    manifest, _ = _manifest(tmp_path)

    with pytest.raises(guard.ReleaseAssetError, match="control policy"):
        _verify_wheels(
            tmp_path / "wheels", manifest, core_options={"root_is_purelib": values}
        )


@pytest.mark.parametrize(
    ("metadata_version", "accepted"),
    [(None, False), ("2.3", False), ("999.0", False), ("2.4", True)],
)
def test_wheel_uses_packaging_validated_core_metadata(
    tmp_path: Path, metadata_version: str | None, accepted: bool
) -> None:
    manifest, _ = _manifest(tmp_path)
    verify = lambda: _verify_wheels(
        tmp_path / str(metadata_version),
        manifest,
        core_options={"metadata_version": metadata_version},
    )

    if accepted:
        verify()
    else:
        with pytest.raises(guard.ReleaseAssetError, match="control metadata|control policy"):
            verify()


@pytest.mark.parametrize(
    "wheel_trailer",
    ["not a valid header\n", "\nunexpected body\n"],
    ids=["malformed-header", "body"],
)
def test_wheel_rejects_malformed_parser_structure(
    tmp_path: Path, wheel_trailer: str
) -> None:
    manifest, _ = _manifest(tmp_path)

    with pytest.raises(guard.ReleaseAssetError, match="control policy"):
        _verify_wheels(
            tmp_path / "wheels",
            manifest,
            core_options={"wheel_trailer": wheel_trailer},
        )


def test_wheel_tags_bind_control_filename_and_declared_policy(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    with pytest.raises(guard.ReleaseAssetError, match="control policy"):
        _verify_wheels(
            tmp_path / "control",
            manifest,
            core_options={"wheel_tags": ("cp312-cp312-linux_x86_64",)},
        )
    platform_tag = "py3-none-manylinux_2_17_x86_64"
    with pytest.raises(guard.ReleaseAssetError, match="release policy"):
        _verify_wheels(
            tmp_path / "policy",
            manifest,
            filename_tag=platform_tag,
            core_options={"wheel_tags": (platform_tag,)},
            memory_options={"wheel_tags": (platform_tag,)},
        )


def test_wheel_policy_checks_every_declared_python_minor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _manifest(tmp_path)
    calls: list[tuple[int, int]] = []
    compatible_tags = packaging_tags.compatible_tags

    def tracking_tags(python_version, interpreter=None, platforms=None):
        calls.append(tuple(python_version))
        return compatible_tags(python_version, interpreter, platforms)

    monkeypatch.setattr(packaging_tags, "compatible_tags", tracking_tags)

    _verify_wheels(tmp_path / "wheels", manifest)

    assert calls == [(3, 10), (3, 11), (3, 12)] * 2


def test_wheel_inspector_rejects_policy_without_declared_source(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "avibe_os-3.1.0-py3-none-any.whl", "avibe-os")
    invented_policy = guard.PackageReleasePolicy(
        ">=3.10", ("3.11+local",), "py3-none-any", 1
    )

    with pytest.raises(guard.ReleaseAssetError, match="inspection policy"):
        guard.inspect_wheel(wheel, policy=invented_policy)


@pytest.mark.parametrize("encrypted", [False, True], ids=["unsupported-compression", "encrypted"])
def test_wheel_translates_unreadable_members_to_asset_failure(
    tmp_path: Path, encrypted: bool
) -> None:
    manifest, _ = _manifest(tmp_path)
    manifest_bytes = manifest.read_bytes()
    policy = guard.load_package_release_policy(
        manifest_bytes, expected_manifest=manifest_bytes, release_tag="v3.1.0"
    )
    wheel = _wheel(tmp_path / "avibe_os-3.1.0-py3-none-any.whl", "avibe-os")
    _make_first_zip_member_unreadable(wheel, encrypted=encrypted)

    with pytest.raises(guard.ReleaseAssetError, match="cannot read wheel"):
        guard.inspect_wheel(wheel, policy=policy)


@pytest.mark.parametrize(
    "member",
    [
        "/absolute",
        "../outside",
        "pkg/./module.py",
        "bad\\path",
        "C:/absolute",
        *(f"pkg/a{character}b" for character in '<>:"|?*'),
        "pkg/trailing.",
        "pkg/trailing ",
        *(f"pkg/{name}" for name in ("CON", "prn.txt", "AUX", "NUL.bin", "COM1", "LPT9.py")),
        "pkg/control\x1f",
    ],
)
def test_wheel_rejects_unsafe_archive_paths(tmp_path: Path, member: str) -> None:
    manifest, _ = _manifest(tmp_path)

    with pytest.raises(guard.ReleaseAssetError, match="archive structure"):
        _verify_wheels(
            tmp_path / "wheels", manifest, core_options={"extra_files": {member: b"x"}}
        )


def test_wheel_rejects_archive_aliases(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)

    with pytest.raises(guard.ReleaseAssetError, match="archive structure"):
        _verify_wheels(
            tmp_path / "wheels",
            manifest,
            core_options={"extra_files": {"AVIBE_OS/__init__.py": b"x"}},
        )


def test_wheel_data_schemes_are_an_explicit_structural_allowlist(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    allowed = {f"avibe_os-3.1.0.data/{scheme}/payload": b"x" for scheme in guard.WHEEL_DATA_SCHEMES}

    _verify_wheels(tmp_path / "allowed", manifest, core_options={"extra_files": allowed})
    for member in (
        "avibe_os-3.1.0.data/unknown/payload",
        "avibe_os-3.1.0.data/scripts",
        "avibe_os-3.1.0.data/scripts/",
        "other-9.9.data/scripts/vibe",
    ):
        with pytest.raises(guard.ReleaseAssetError, match="archive structure"):
            _verify_wheels(
                tmp_path / member.replace("/", "-"),
                manifest,
                core_options={"extra_files": {member: b"x"}},
            )


def test_wheel_rejects_raw_nul_member_name_before_zipfile_sanitization(
    tmp_path: Path,
) -> None:
    manifest, _ = _manifest(tmp_path)
    manifest_bytes = manifest.read_bytes()
    policy = guard.load_package_release_policy(
        manifest_bytes, expected_manifest=manifest_bytes, release_tag="v3.1.0"
    )
    wheel = _wheel(
        tmp_path / "avibe_os-3.1.0-py3-none-any.whl",
        "avibe-os",
        extra_files={"evilXtail": b"x"},
    )
    wheel.write_bytes(wheel.read_bytes().replace(b"evilXtail", b"evil\x00tail"))

    with zipfile.ZipFile(wheel) as archive:
        member = next(info for info in archive.infolist() if info.filename == "evil")
        assert member.orig_filename == "evil\x00tail"

    with pytest.raises(guard.ReleaseAssetError, match="archive structure"):
        guard.inspect_wheel(wheel, policy=policy)


@pytest.mark.parametrize("ancestor", ["conflict", "Conflict"], ids=["exact", "casefold"])
def test_wheel_rejects_file_and_descendant_topology(
    tmp_path: Path, ancestor: str
) -> None:
    manifest, _ = _manifest(tmp_path)

    with pytest.raises(guard.ReleaseAssetError, match="archive structure"):
        _verify_wheels(
            tmp_path / "wheels",
            manifest,
            core_options={"extra_files": {ancestor: b"x", "conflict/child": b"y"}},
        )


def test_wheel_topology_uses_one_binary_search_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _wheel(
        tmp_path / "avibe_os-3.1.0-py3-none-any.whl",
        "avibe-os",
        extra_files={f"payload/item-{index:04d}": b"" for index in range(256)},
    )
    calls = 0
    bisect_left = guard.bisect_left

    def tracking_bisect(values: list[str], prefix: str) -> int:
        nonlocal calls
        calls += 1
        return bisect_left(values, prefix)

    monkeypatch.setattr(guard, "bisect_left", tracking_bisect)
    with zipfile.ZipFile(wheel) as archive:
        inventory, _ = guard._validate_wheel_archive(
            archive, wheel, "avibe_os-3.1.0.dist-info"
        )

    assert calls == sum(not entry.is_dir for entry in inventory)


@pytest.mark.parametrize(
    ("declared_size", "payload"),
    [(1, b""), (0, b"x")],
    ids=["declared-payload", "observed-payload"],
)
def test_wheel_rejects_payload_bearing_directory_member(
    tmp_path: Path, declared_size: int, payload: bytes
) -> None:
    member = zipfile.ZipInfo("payload/")
    member.file_size = declared_size

    class Archive:
        @staticmethod
        def open(_member: zipfile.ZipInfo) -> io.BytesIO:
            return io.BytesIO(payload)

    with pytest.raises(guard.ReleaseAssetError, match="member size"):
        guard._inventory_wheel_member(
            Archive(),
            member,
            tmp_path / "wheel.whl",
            "payload",
            retain=False,
        )


def test_wheel_requires_raw_record_control(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)

    with pytest.raises(guard.ReleaseAssetError, match="control structure"):
        _verify_wheels(
            tmp_path / "wheels", manifest, core_options={"include_record": False}
        )


def test_wheel_rejects_duplicate_raw_record_control(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)

    with pytest.warns(UserWarning, match="Duplicate name"):
        with pytest.raises(
            guard.ReleaseAssetError, match="archive structure|control structure"
        ):
            _verify_wheels(
                tmp_path / "wheels",
                manifest,
                core_options={"duplicate_control": "RECORD"},
            )


def test_wheel_inventory_retains_raw_record_without_interpreting_ledger(
    tmp_path: Path,
) -> None:
    manifest, _ = _manifest(tmp_path)
    opaque_record = b"\xffnot,a,ledger\n"
    wheels = tmp_path / "wheels"

    _verify_wheels(wheels, manifest, core_options={"record_bytes": opaque_record})
    wheel = wheels / "avibe_os-3.1.0-py3-none-any.whl"
    dist_info = "avibe_os-3.1.0.dist-info"
    record = f"{dist_info}/RECORD"
    with zipfile.ZipFile(wheel) as archive:
        inventory, retained = guard._validate_wheel_archive(archive, wheel, dist_info)
    entry = next(item for item in inventory if item.path == record)

    assert retained[record] == opaque_record
    assert entry.size == len(opaque_record)
    assert entry.sha256 == base64.urlsafe_b64encode(
        hashlib.sha256(opaque_record).digest()
    ).rstrip(b"=").decode()


@pytest.mark.parametrize(
    "compression",
    [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED],
    ids=["oversized", "high-compression"],
)
def test_wheel_bounds_every_member_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compression: int
) -> None:
    manifest, _ = _manifest(tmp_path)
    monkeypatch.setattr(guard, "MAX_WHEEL_MEMBER_BYTES", 32)

    with pytest.raises(guard.ReleaseAssetError, match="member size"):
        _verify_wheels(
            tmp_path / "wheels",
            manifest,
            core_options={"extra_files": {"payload.bin": b"x" * 33}, "compression": compression},
        )


def test_wheel_reads_every_member_for_integrity(tmp_path: Path) -> None:
    manifest, _ = _manifest(tmp_path)
    wheel = _wheel(tmp_path / "avibe_os-3.1.0-py3-none-any.whl", "avibe-os")
    wheel.write_bytes(wheel.read_bytes().replace(b"x = 1\n", b"x = 2\n"))
    manifest_bytes = manifest.read_bytes()
    policy = guard.load_package_release_policy(
        manifest_bytes, expected_manifest=manifest_bytes, release_tag="v3.1.0"
    )

    with pytest.raises(guard.ReleaseAssetError, match="cannot read wheel"):
        guard.inspect_wheel(wheel, policy=policy)


def test_wheel_archive_discards_payload_bytes_after_integrity_read(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "avibe_os-3.1.0-py3-none-any.whl", "avibe-os")
    dist_info = "avibe_os-3.1.0.dist-info"

    with zipfile.ZipFile(wheel) as archive:
        _, retained = guard._validate_wheel_archive(archive, wheel, dist_info)

    assert set(retained) == {f"{dist_info}/{name}" for name in ("METADATA", "WHEEL", "RECORD")}


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
