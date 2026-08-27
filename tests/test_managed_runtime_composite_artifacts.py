from __future__ import annotations

import hashlib
import io
import json
import tarfile
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core import managed_runtime, show_runtime, tmux_runtime
from core.managed_runtime import ManagedRuntimeManager, ManagedRuntimeSpec


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "show_runtime" / "v3.0.13"
CLI_PAYLOAD = b"#!/usr/bin/env node\n"
ESBUILD_PAYLOAD = b"fixture-esbuild\n"
Extractor = Callable[[tarfile.TarFile, Path], None]
EXTRACTORS: tuple[tuple[str, Extractor], ...] = (
    ("shared", managed_runtime.safe_extract_tar),
    ("show", show_runtime._safe_extract_tar),
    ("tmux", tmux_runtime._safe_extract_tar),
)


class _CompositeFixtureManager(ManagedRuntimeManager):
    def __init__(self, *, spec: ManagedRuntimeSpec, runtime_dir: Path, manifest_path: Path, version: str) -> None:
        super().__init__(spec=spec, runtime_dir=runtime_dir, manifest_path=manifest_path)
        self._version = version

    def _binary_version(self, binary: Path | None) -> str | None:
        return self._version if binary and binary.is_file() else None


def _released_fixture() -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / "composite_artifacts.json").read_text(encoding="utf-8"))


def _payload_for(name: str) -> bytes:
    if name.endswith("dist/cli.js"):
        return CLI_PAYLOAD
    if "esbuild" in name:
        return ESBUILD_PAYLOAD
    return f"released-shape:{name}\n".encode()


def _write_released_shape_archive(tmp_path: Path, platform: str, fixture: dict[str, Any]) -> Path:
    links = fixture["links"]
    link_names = {row[2] for row in links}
    members: dict[str, tuple[int, str]] = {}

    for row in links:
        resolved_index, resolved_name, resolved_type = row[5:8]
        if resolved_name not in link_names:
            members[resolved_name] = (resolved_index, resolved_type)
    for index, name in fixture["entrypoints"].values():
        if index is not None and name not in link_names:
            members[name] = (index, "file")

    events: list[tuple[int, int, str, Any]] = []
    events.extend((index, 0, name, member_type) for name, (index, member_type) in members.items())
    events.extend((row[0], 1, row[2], row) for row in links)
    archive_path = tmp_path / f"{platform}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for _index, event_type, name, details in sorted(events):
            member = tarfile.TarInfo(name)
            if event_type == 0 and details == "directory":
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
                archive.addfile(member)
            elif event_type == 0:
                payload = _payload_for(name)
                member.size = len(payload)
                member.mode = 0o755
                archive.addfile(member, io.BytesIO(payload))
            else:
                member.type = tarfile.SYMTYPE if details[1] == "symlink" else tarfile.LNKTYPE
                member.linkname = details[3]
                member.mode = 0o755
                archive.addfile(member)
    return archive_path


@pytest.mark.parametrize(
    ("platform", "expected_forward_symlinks"),
    (
        ("darwin-arm64", 2),
        ("darwin-x64", 2),
        ("linux-arm64", 3),
        ("linux-x64", 2),
        ("win32-arm64", 0),
        ("win32-x64", 0),
    ),
)
def test_released_show_links_install_through_shared_manager(
    tmp_path: Path,
    platform: str,
    expected_forward_symlinks: int,
) -> None:
    fixture = _released_fixture()
    release = fixture["release"]
    released_manifest_bytes = (FIXTURE_ROOT / release["manifest_name"]).read_bytes()
    assert hashlib.sha256(released_manifest_bytes).hexdigest() == release["manifest_sha256"]
    released_manifest = json.loads(released_manifest_bytes)
    assert release["tag"] == "v3.0.13"
    assert released_manifest["runtime_version"] == release["runtime_version"]

    archive_fixture = fixture["archives"][platform]
    provenance = archive_fixture["provenance"]
    released_archive = released_manifest["archives"][platform]
    assert {
        "name": released_archive["name"],
        "sha256": released_archive["sha256"],
        "size": released_archive["size"],
    } == {key: provenance[key] for key in ("name", "sha256", "size")}

    links = archive_fixture["links"]
    symlinks = [row for row in links if row[1] == "symlink"]
    hardlinks = [row for row in links if row[1] == "hardlink"]
    assert all(
        0 <= index < provenance["member_count"]
        for row in links
        for index in (row[0], row[5])
    )
    if platform.startswith("win32"):
        assert links == []
    else:
        assert len(symlinks) == 16
        assert len(hardlinks) == 1
        assert all(row[4] is not None and row[4] < row[0] for row in hardlinks)
    assert sum(row[4] is not None and row[4] > row[0] for row in symlinks) == expected_forward_symlinks

    archive_path = _write_released_shape_archive(tmp_path, platform, archive_fixture)
    entrypoints = archive_fixture["entrypoints"]
    cli_path = entrypoints["cli_path"][1]
    manifest_payload = {
        "schema_version": 1,
        "runtime_version": release["runtime_version"],
        "source": f"released-shape:{release['tag']}",
        "archives": {
            platform: {
                "name": archive_path.name,
                "url": archive_path.as_uri(),
                "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "binary_sha256": hashlib.sha256(CLI_PAYLOAD).hexdigest(),
                "size": archive_path.stat().st_size,
                "bin_path": cli_path,
            }
        },
    }
    manifest_path = tmp_path / f"{platform}-manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    manager = _CompositeFixtureManager(
        spec=ManagedRuntimeSpec(
            runtime_id=f"show-fixture-{platform}",
            manifest_resource="unused.json",
            version_field="runtime_version",
            default_bin_path=cli_path,
            platform_aliases=((managed_runtime.runtime_platform_tag(), platform),),
        ),
        runtime_dir=tmp_path / f"runtime-{platform}",
        manifest_path=manifest_path,
        version=release["runtime_version"],
    )

    result = manager.ensure()

    assert result["ok"] is True
    install_dir = Path(result["install_dir"])
    assert (install_dir / cli_path).read_bytes() == CLI_PAYLOAD
    assert manager.resolve_binary() == (install_dir / cli_path).resolve()
    for key in ("esbuild_bin", "esbuild_package", "esbuild_platform"):
        assert (install_dir / entrypoints[key][1]).read_bytes() == ESBUILD_PAYLOAD
    for row in symlinks:
        assert (install_dir / row[2]).is_symlink()
        assert (install_dir / row[2]).exists()
    for row in hardlinks:
        assert (install_dir / row[2]).stat().st_ino == (install_dir / row[6]).stat().st_ino


@pytest.mark.parametrize(
    "platform",
    (
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64",
        "linux-x64",
        "win32-arm64",
        "win32-x64",
    ),
)
def test_released_show_composite_shape_installs_through_production_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
) -> None:
    fixture = _released_fixture()
    release = fixture["release"]
    released_manifest_bytes = (FIXTURE_ROOT / release["manifest_name"]).read_bytes()
    assert hashlib.sha256(released_manifest_bytes).hexdigest() == release["manifest_sha256"]
    released_manifest = json.loads(released_manifest_bytes)
    released_archive = released_manifest["archives"][platform]
    provenance = fixture["archives"][platform]["provenance"]
    assert {
        "name": released_archive["name"],
        "sha256": released_archive["sha256"],
        "size": released_archive["size"],
    } == {key: provenance[key] for key in ("name", "sha256", "size")}

    archive_fixture = fixture["archives"][platform]
    archive_path = _write_released_shape_archive(tmp_path, platform, archive_fixture)
    cli_path = archive_fixture["entrypoints"]["cli_path"][1]
    manifest_path = tmp_path / f"{platform}-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": released_manifest["schema_version"],
                "runtime_version": released_manifest["runtime_version"],
                "minimum_node": released_manifest["minimum_node"],
                "archives": {
                    platform: {
                        "name": archive_path.name,
                        "url": archive_path.as_uri(),
                        "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                        "size": archive_path.stat().st_size,
                        "bin_path": cli_path,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: platform)
    monkeypatch.setattr(show_runtime, "_runtime_platform_tag", lambda: platform)
    monkeypatch.setattr(
        show_runtime,
        "_resolve_command",
        lambda command: ["/bin/node"] if command == "node" else None,
    )
    monkeypatch.setattr(show_runtime, "_node_version", lambda _node: (22, 12, 0))
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    runtime_dir = tmp_path / f"runtime-{platform}"
    manager = show_runtime.ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
    )

    result = manager.prepare()

    assert result["ok"] is True
    pointer = json.loads((runtime_dir / "current.json").read_text(encoding="utf-8"))
    install_dir = Path(pointer["install_dir"])
    assert result["command"] == ["/bin/node", str(install_dir / cli_path)]
    assert (install_dir / cli_path).read_bytes() == CLI_PAYLOAD
    for key in ("esbuild_bin", "esbuild_package", "esbuild_platform"):
        assert (install_dir / archive_fixture["entrypoints"][key][1]).read_bytes() == ESBUILD_PAYLOAD
    for row in archive_fixture["links"]:
        linked = install_dir / row[2]
        if row[1] == "symlink":
            assert linked.is_symlink()
            assert linked.exists()
        else:
            assert linked.stat().st_ino == (install_dir / row[6]).stat().st_ino


def _write_link_probe(path: Path, *, escaping: bool = False) -> None:
    with tarfile.open(path, "w") as archive:
        if escaping:
            link = tarfile.TarInfo("root/escape")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../outside"
            archive.addfile(link)
            return
        payload = b"composite\n"
        regular = tarfile.TarInfo("root/regular")
        regular.size = len(payload)
        regular.mode = 0o644
        archive.addfile(regular, io.BytesIO(payload))
        symlink = tarfile.TarInfo("root/symlink")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "regular"
        archive.addfile(symlink)
        hardlink = tarfile.TarInfo("root/hardlink")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "root/regular"
        archive.addfile(hardlink)


@pytest.mark.parametrize(("_name", "extractor"), EXTRACTORS, ids=[item[0] for item in EXTRACTORS])
def test_extractor_behavior_accepts_composite_members_and_rejects_escape(
    tmp_path: Path,
    _name: str,
    extractor: Extractor,
) -> None:
    benign = tmp_path / "benign.tar"
    _write_link_probe(benign)
    destination = tmp_path / "benign"
    with tarfile.open(benign) as archive:
        extractor(archive, destination)
    assert (destination / "root/symlink").read_bytes() == b"composite\n"
    assert (destination / "root/hardlink").stat().st_ino == (destination / "root/regular").stat().st_ino

    escaping = tmp_path / "escaping.tar"
    _write_link_probe(escaping, escaping=True)
    escaped_destination = tmp_path / "escaping"
    with tarfile.open(escaping) as archive:
        with pytest.raises((ValueError, tarfile.FilterError)):
            extractor(archive, escaped_destination)
    assert not escaped_destination.exists()
    assert not (tmp_path / "outside").exists()


def _write_ordering_probe(path: Path) -> None:
    with tarfile.open(path, "w") as archive:
        payload = b"archive-inside\n"
        regular = tarfile.TarInfo("outside")
        regular.size = len(payload)
        regular.mode = 0o644
        archive.addfile(regular, io.BytesIO(payload))
        directory_link = tarfile.TarInfo("pivot")
        directory_link.type = tarfile.SYMTYPE
        directory_link.linkname = "."
        archive.addfile(directory_link)
        hardlink = tarfile.TarInfo("inside-hard")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "pivot/../outside"
        archive.addfile(hardlink)


def _write_fallback_escape_probe(path: Path) -> None:
    with tarfile.open(path, "w") as archive:
        directory_link = tarfile.TarInfo("a")
        directory_link.type = tarfile.SYMTYPE
        directory_link.linkname = "."
        archive.addfile(directory_link)
        escaping_link = tarfile.TarInfo("a/x")
        escaping_link.type = tarfile.SYMTYPE
        escaping_link.linkname = "../victim"
        archive.addfile(escaping_link)
        payload = b"OVERWRITTEN\n"
        regular = tarfile.TarInfo("x")
        regular.size = len(payload)
        archive.addfile(regular, io.BytesIO(payload))


def test_shared_fallback_rejects_symlink_pivot_before_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "fallback-escape.tar"
    _write_fallback_escape_probe(archive_path)
    case = tmp_path / "fallback"
    case.mkdir()
    destination = case / "destination"
    victim = case / "victim"
    victim.write_bytes(b"ORIGINAL\n")
    victim_stat = victim.stat()
    monkeypatch.delattr(tarfile, "data_filter", raising=False)

    with tarfile.open(archive_path) as archive:
        with pytest.raises(
            ValueError,
            match=r"^Managed runtime archive path/type collision: a/x$",
        ):
            managed_runtime.safe_extract_tar(archive, destination)

    assert victim.read_bytes() == b"ORIGINAL\n"
    assert victim.stat().st_ino == victim_stat.st_ino
    assert victim.stat().st_nlink == victim_stat.st_nlink
    assert not destination.exists()


def test_shared_fallback_materializes_confined_hardlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "fallback-hardlink.tar"
    with tarfile.open(archive_path, "w") as archive:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        payload = b"payload\n"
        regular = tarfile.TarInfo("root/regular")
        regular.size = len(payload)
        archive.addfile(regular, io.BytesIO(payload))
        hardlink = tarfile.TarInfo("root/hardlink")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "root/regular"
        archive.addfile(hardlink)
    destination = tmp_path / "fallback-hardlink"
    monkeypatch.delattr(tarfile, "data_filter", raising=False)

    with tarfile.open(archive_path) as archive:
        managed_runtime.safe_extract_tar(archive, destination)

    assert (destination / "root/regular").read_bytes() == payload
    assert (destination / "root/hardlink").stat().st_ino == (
        destination / "root/regular"
    ).stat().st_ino


def test_shared_fallback_rejects_hardlink_target_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "fallback-hardlink-substitution.tar"
    with tarfile.open(archive_path, "w") as archive:
        payload = b"payload\n"
        regular = tarfile.TarInfo("root/regular")
        regular.size = len(payload)
        archive.addfile(regular, io.BytesIO(payload))
        symlink = tarfile.TarInfo("root/symlink")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "regular"
        archive.addfile(symlink)
        hardlink = tarfile.TarInfo("root/hardlink")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "root/symlink"
        archive.addfile(hardlink)
    destination = tmp_path / "fallback-hardlink-substitution"
    monkeypatch.delattr(tarfile, "data_filter", raising=False)

    with tarfile.open(archive_path) as archive:
        with pytest.raises(
            ValueError,
            match=r"^Unsafe managed runtime archive hardlink target: root/hardlink$",
        ):
            managed_runtime.safe_extract_tar(archive, destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    "invalid_shape",
    ("unsupported", "unsafe-path", "duplicate-path", "symlink-cycle"),
)
def test_shared_fallback_rejects_invalid_archive_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_shape: str,
) -> None:
    archive_path = tmp_path / f"fallback-{invalid_shape}.tar"
    with tarfile.open(archive_path, "w") as archive:
        if invalid_shape == "unsupported":
            member = tarfile.TarInfo("root/fifo")
            member.type = tarfile.FIFOTYPE
            archive.addfile(member)
        elif invalid_shape == "unsafe-path":
            member = tarfile.TarInfo("../outside")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        elif invalid_shape == "duplicate-path":
            for name in ("root/file", "root/./file"):
                member = tarfile.TarInfo(name)
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))
        else:
            first = tarfile.TarInfo("root/first")
            first.type = tarfile.SYMTYPE
            first.linkname = "second"
            archive.addfile(first)
            second = tarfile.TarInfo("root/second")
            second.type = tarfile.SYMTYPE
            second.linkname = "first"
            archive.addfile(second)
    destination = tmp_path / "destination"
    monkeypatch.delattr(tarfile, "data_filter", raising=False)

    with tarfile.open(archive_path) as archive:
        with pytest.raises(ValueError):
            managed_runtime.safe_extract_tar(archive, destination)

    assert not destination.exists()
    assert not (tmp_path / "outside").exists()


def test_shared_fallback_keeps_order_dependent_links_confined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "fallback-ordering.tar"
    _write_ordering_probe(archive_path)
    case = tmp_path / "fallback-ordering"
    destination = case / "destination"
    destination.mkdir(parents=True)
    outside = case / "outside"
    outside.write_bytes(b"outside\n")
    outside_stat = outside.stat()
    monkeypatch.delattr(tarfile, "data_filter", raising=False)

    with tarfile.open(archive_path) as archive:
        managed_runtime.safe_extract_tar(archive, destination)

    assert outside.read_bytes() == b"outside\n"
    assert outside.stat().st_ino == outside_stat.st_ino
    assert outside.stat().st_nlink == outside_stat.st_nlink
    assert (destination / "inside-hard").stat().st_ino == (
        destination / "outside"
    ).stat().st_ino
    assert (destination / "inside-hard").stat().st_ino != outside.stat().st_ino


@pytest.mark.parametrize(
    ("_name", "extractor"),
    (
        ("shared", managed_runtime.safe_extract_tar),
        ("show", show_runtime._safe_extract_tar),
        ("tarfile", lambda archive, destination: archive.extractall(destination, filter="data")),
    ),
)
def test_order_dependent_link_target_stays_confined(
    tmp_path: Path,
    _name: str,
    extractor: Extractor,
) -> None:
    if not hasattr(tarfile, "data_filter"):
        pytest.skip("data filter unavailable on this deferred fallback interpreter")
    archive_path = tmp_path / "ordering.tar"
    _write_ordering_probe(archive_path)
    case = tmp_path / _name
    destination = case / "destination"
    destination.mkdir(parents=True)
    outside = case / "outside"
    outside.write_bytes(b"outside\n")
    outside_stat = outside.stat()

    extraction_error = None
    with tarfile.open(archive_path) as archive:
        try:
            extractor(archive, destination)
        except (KeyError, tarfile.LinkOutsideDestinationError) as error:
            extraction_error = error

    assert outside.read_bytes() == b"outside\n"
    assert outside.stat().st_ino == outside_stat.st_ino
    assert outside.stat().st_nlink == outside_stat.st_nlink
    assert (destination / "outside").read_bytes() == b"archive-inside\n"
    assert (destination / "pivot").is_symlink()
    extracted_hardlink = destination / "inside-hard"
    if extraction_error is None:
        assert extracted_hardlink.stat().st_ino == (destination / "outside").stat().st_ino
        assert extracted_hardlink.stat().st_ino != outside.stat().st_ino
        expected_members = ["inside-hard", "outside", "pivot"]
    else:
        assert not extracted_hardlink.exists()
        expected_members = ["outside", "pivot"]
    assert sorted(path.name for path in destination.iterdir()) == expected_members


@pytest.mark.parametrize(
    ("_name", "extractor", "detects_before_extract"),
    (
        ("shared", managed_runtime.safe_extract_tar, True),
        ("show", show_runtime._safe_extract_tar, False),
        ("tmux", tmux_runtime._safe_extract_tar, True),
    ),
)
@pytest.mark.parametrize("supports_filter", (True, False), ids=("available", "unavailable"))
def test_filter_capability_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _name: str,
    extractor: Extractor,
    detects_before_extract: bool,
    supports_filter: bool,
) -> None:
    archive_path = tmp_path / f"{_name}-{supports_filter}.tar"
    payload = b"filtered\n"
    with tarfile.open(archive_path, "w") as archive:
        member = tarfile.TarInfo("payload")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    destination = tmp_path / f"{_name}-{supports_filter}"
    actual_filter_available = hasattr(tarfile, "data_filter")
    if detects_before_extract:
        if supports_filter:
            monkeypatch.setattr(tarfile, "data_filter", object(), raising=False)
        else:
            monkeypatch.delattr(tarfile, "data_filter", raising=False)
    calls: list[object] = []

    with tarfile.open(archive_path) as archive:
        original_extractall = archive.extractall

        def capture_extractall(path, members=None, *, numeric_owner=False, filter=None):
            calls.append(filter)
            if filter is not None and not supports_filter and not detects_before_extract:
                raise TypeError("filter is unavailable")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                return original_extractall(
                    path,
                    members=members,
                    numeric_owner=numeric_owner,
                    **(
                        {"filter": filter}
                        if filter is not None and actual_filter_available
                        else {}
                    ),
                )

        monkeypatch.setattr(archive, "extractall", capture_extractall)
        extractor(archive, destination)

    if supports_filter:
        expected_calls = ["data"]
    elif _name == "shared":
        expected_calls = []
    elif detects_before_extract:
        expected_calls = [None]
    else:
        expected_calls = ["data", None]
    assert calls == expected_calls
    assert (destination / "payload").read_bytes() == payload
