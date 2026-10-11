from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from core import managed_runtime
from core.agent_path import prepend_managed_tools_to_path
from core.ripgrep_runtime import RipgrepRuntimeManager, prepend_managed_ripgrep_to_path

UPSTREAM = "https://github.com/BurntSushi/ripgrep/releases/download"


def pin_test_ripgrep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, tamper: str | None = None, archive_format: str = "tar.gz"
) -> None:
    """Pin a stand-in ``rg`` archive in a test manifest the default manager also reads."""

    stem = "ripgrep-15.2.0-test"
    binary = tmp_path / "archive-root" / stem / "rg"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\necho "ripgrep 15.2.0 (rev test)"\n', encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    archive = tmp_path / f"{stem}.{archive_format}"
    if archive_format == "zip":
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.write(binary, arcname=f"{stem}/rg")
    else:
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(binary, arcname=f"{stem}/rg")
    entry = {
        "name": archive.name,
        "url": archive.as_uri(),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "size": archive.stat().st_size,
        "bin_path": f"{stem}/rg",
        "format": archive_format,
    }
    if tamper is not None:
        entry[tamper] = "0" * 64
    manifest = tmp_path / "ripgrep_runtime_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ripgrep_version": "15.2.0",
                "source": "test",
                "release_tag": "15.2.0",
                "archives": {managed_runtime.runtime_platform_tag(): entry},
            }
        ),
        encoding="utf-8",
    )
    # The runtime installs under the test's own AVIBE_HOME (conftest).
    monkeypatch.setenv("VIBE_RIPGREP_MANIFEST_PATH", str(manifest))


def install_test_ripgrep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, tamper: str | None = None, archive_format: str = "tar.gz"
) -> dict:
    """Install a stand-in ``rg`` through a test manifest the default manager also reads."""

    pin_test_ripgrep(tmp_path, monkeypatch, tamper=tamper, archive_format=archive_format)
    return RipgrepRuntimeManager().ensure()


def test_packaged_manifest_pins_the_official_six_platform_release(tmp_path: Path) -> None:
    manifest = RipgrepRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)._load_manifest(
        allow_network=False
    )

    assert manifest is not None
    assert manifest.runtime_version == manifest.payload["release_tag"] == "15.2.0"
    targets = {
        "darwin-arm64": "aarch64-apple-darwin",
        "darwin-x64": "x86_64-apple-darwin",
        "linux-arm64": "aarch64-unknown-linux-musl",
        "linux-x64": "x86_64-unknown-linux-musl",
        "win32-arm64": "aarch64-pc-windows-msvc",
        "win32-x64": "x86_64-pc-windows-msvc",
    }
    assert set(manifest.archives) == set(targets)
    for platform_tag, archive in manifest.archives.items():
        stem = f"ripgrep-15.2.0-{targets[platform_tag]}"
        windows = platform_tag.startswith("win32-")
        assert (archive.name, archive.format) == ((f"{stem}.zip", "zip") if windows else (f"{stem}.tar.gz", "tar.gz"))
        assert archive.url == f"{UPSTREAM}/15.2.0/{archive.name}"
        assert archive.bin_path == f"{stem}/{'rg.exe' if windows else 'rg'}"
        assert archive.size is not None and archive.size > 0
        assert re.fullmatch(r"[0-9a-f]{64}", archive.sha256)
        assert re.fullmatch(r"[0-9a-f]{64}", archive.binary_sha256 or "")


@pytest.mark.skipif(os.name == "nt", reason="the stand-in rg is a shell script")
@pytest.mark.parametrize("archive_format", ("tar.gz", "zip"))
@pytest.mark.parametrize(
    ("tamper", "reason"),
    (
        (None, None),
        ("sha256", "ripgrep_archive_checksum_mismatch"),
        ("binary_sha256", "ripgrep_binary_checksum_mismatch"),
    ),
)
def test_install_accepts_only_the_bytes_the_manifest_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str | None, reason: str | None, archive_format: str
) -> None:
    result = install_test_ripgrep(tmp_path, monkeypatch, tamper=tamper, archive_format=archive_format)

    assert result.get("reason") == reason
    resolved = RipgrepRuntimeManager().resolve_binary()
    if reason is None:
        assert result["ok"] is True and result["version"] == "15.2.0"
        assert resolved == Path(result["path"]) and resolved.name == "rg"
    else:
        assert result["ok"] is False and resolved is None


def test_agent_path_gains_managed_rg_only_where_path_finds_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = Path(install_test_ripgrep(tmp_path, monkeypatch)["path"])
    without_rg = tmp_path / "bin-without-rg"
    without_rg.mkdir()
    with_rg = tmp_path / "bin-with-rg"
    with_rg.mkdir()
    (with_rg / "rg").write_text("#!/bin/sh\n", encoding="utf-8")
    (with_rg / "rg").chmod(0o755)

    # Absent key: the base environment's PATH, behind the managed rg.
    env: dict[str, str] = {}
    assert prepend_managed_ripgrep_to_path(env, base_env={"PATH": str(without_rg)}) is True
    assert env["PATH"] == f"{installed.parent}{os.pathsep}{without_rg}"

    # The user's own rg on PATH is kept.
    env = {"PATH": str(with_rg)}
    assert prepend_managed_ripgrep_to_path(env, base_env={"PATH": str(without_rg)}) is False
    assert env == {"PATH": str(with_rg)}

    # Every backend composes Git and ripgrep through one call.
    env = {"PATH": str(without_rg)}
    assert prepend_managed_tools_to_path(env, base_env={}, working_dir=tmp_path) is True
    assert env["PATH"].split(os.pathsep)[0] == str(installed.parent)


def test_agent_path_is_unchanged_without_a_verified_managed_rg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = Path(install_test_ripgrep(tmp_path, monkeypatch)["path"])
    installed.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    without_rg = tmp_path / "bin-without-rg"
    without_rg.mkdir()
    env = {"PATH": str(without_rg)}

    assert prepend_managed_ripgrep_to_path(env, base_env={}) is False
    assert env == {"PATH": str(without_rg)}


@pytest.mark.parametrize("tamper", (None, "sha256"))
def test_startup_reconcile_installs_ripgrep_without_failing_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str | None
) -> None:
    """The desktop Runtime never runs `vibe runtime prepare`; service startup must install rg instead."""

    import core.show_runtime as show_runtime
    from vibe import api

    for ensure in ("ensure_askill_installed", "ensure_avault_installed", "ensure_model_hub_engine_installed"):
        monkeypatch.setattr(api, ensure, lambda force=False: {"ok": True, "installed": True})

    class _ReadyShowRuntime:
        def status(self, *, offline=False):
            return {"node_available": True, "node_supported": True, "node_version": "22.12.0"}

        def prepare(self, *, force=False, automatic=False):
            return {"policy": {"state": "allowed"}, "install": {"state": "installed"}}

    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: _ReadyShowRuntime())
    monkeypatch.setenv("VIBE_INSTALL_SKIP_TMUX", "1")
    pin_test_ripgrep(tmp_path, monkeypatch, tamper=tamper)
    assert RipgrepRuntimeManager().resolve_binary() is None

    out = api.reconcile_startup_dependencies()

    resolved = RipgrepRuntimeManager().resolve_binary()
    if tamper is None:
        assert out["ripgrep"]["ok"] is True
        assert resolved is not None and resolved == Path(out["ripgrep"]["path"])
    else:
        assert out["ripgrep"]["ok"] is False
        assert out["ripgrep"]["reason"] == "ripgrep_archive_checksum_mismatch"
        assert resolved is None
    # A missing ripgrep only changes Vibey's prompt line, so it never fails startup.
    assert out["ok"] is True


@pytest.mark.parametrize(
    "member",
    ("../escape", "/absolute", "C:/drive", "inside/../../escape", ".. /escape", "inside/name.", "link"),
)
def test_a_zip_entry_that_leaves_the_runtime_or_links_out_is_refused(tmp_path: Path, member: str) -> None:
    """The same rules ``safe_extract_tar`` applies: nothing lands outside the staging directory, and no links."""
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        if member == "link":
            info = zipfile.ZipInfo("runtime/rg")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zipped.writestr(info, "/etc/passwd")
        else:
            zipped.writestr(zipfile.ZipInfo(member), "x")
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(ValueError):
        managed_runtime.stage_archive(archive, "zip", staging, bin_path="runtime/rg")

    assert list(staging.iterdir()) == []
    assert not (tmp_path / "escape").exists()


@pytest.mark.skipif(
    not os.environ.get("AVIBE_TEST_RIPGREP_ZIPS"),
    reason="needs ripgrep's Windows zips downloaded into AVIBE_TEST_RIPGREP_ZIPS (CI's Windows jobs)",
)
def test_the_pinned_windows_zip_installs_and_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bytes the packaged manifest pins for this Windows machine, from a local copy of upstream's zip."""
    packaged = RipgrepRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)._load_manifest(
        allow_network=False
    )
    tag = managed_runtime.runtime_platform_tag()
    payload = json.loads(json.dumps(packaged.payload))
    entry = payload["archives"][tag]
    entry["url"] = (Path(os.environ["AVIBE_TEST_RIPGREP_ZIPS"]) / entry["name"]).as_uri()
    manifest = tmp_path / "ripgrep_runtime_manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("VIBE_RIPGREP_MANIFEST_PATH", str(manifest))

    result = RipgrepRuntimeManager().ensure()

    assert result["ok"] is True and result["version"] == "15.2.0"
    binary = RipgrepRuntimeManager().resolve_binary()
    assert binary is not None and binary.name == "rg.exe"
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30, check=True)
    assert version.stdout.startswith("ripgrep 15.2.0")
    env: dict[str, str] = {"PATH": os.environ.get("SystemRoot", r"C:\Windows")}
    assert prepend_managed_ripgrep_to_path(env, base_env={}) is True
    assert env["PATH"].split(os.pathsep)[0] == str(binary.parent)


def test_a_zip_that_records_no_file_types_extracts(tmp_path: Path) -> None:
    """A zip need not record a Unix file type: ``writestr`` gives a file a bare permission mode."""
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("runtime/", "")
        info = zipfile.ZipInfo("runtime/rg")
        info.external_attr = 0o600 << 16
        zipped.writestr(info, "binary")
    staging = tmp_path / "staging"
    staging.mkdir()

    managed_runtime.stage_archive(archive, "zip", staging, bin_path="runtime/rg")

    assert (staging / "runtime" / "rg").read_text(encoding="utf-8") == "binary"
