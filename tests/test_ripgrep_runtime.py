from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tarfile
from pathlib import Path

import pytest

from core import managed_runtime
from core.agent_path import prepend_managed_tools_to_path
from core.ripgrep_runtime import RipgrepRuntimeManager, prepend_managed_ripgrep_to_path

UPSTREAM = "https://github.com/BurntSushi/ripgrep/releases/download"


def install_test_ripgrep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, tamper: str | None = None) -> dict:
    """Install a stand-in ``rg`` through a test manifest the default manager also reads."""

    stem = "ripgrep-15.2.0-test"
    binary = tmp_path / "archive-root" / stem / "rg"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\necho "ripgrep 15.2.0 (rev test)"\n', encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    archive = tmp_path / f"{stem}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname=f"{stem}/rg")
    entry = {
        "name": archive.name,
        "url": archive.as_uri(),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "size": archive.stat().st_size,
        "bin_path": f"{stem}/rg",
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
    return RipgrepRuntimeManager().ensure()


def test_packaged_manifest_pins_the_official_four_platform_release(tmp_path: Path) -> None:
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
    }
    assert set(manifest.archives) == set(targets)
    for platform_tag, archive in manifest.archives.items():
        stem = f"ripgrep-15.2.0-{targets[platform_tag]}"
        assert archive.name == f"{stem}.tar.gz"
        assert archive.url == f"{UPSTREAM}/15.2.0/{archive.name}"
        assert archive.bin_path == f"{stem}/rg"
        assert archive.size is not None and archive.size > 0
        assert re.fullmatch(r"[0-9a-f]{64}", archive.sha256)
        assert re.fullmatch(r"[0-9a-f]{64}", archive.binary_sha256 or "")


@pytest.mark.parametrize(
    ("tamper", "reason"),
    (
        (None, None),
        ("sha256", "ripgrep_archive_checksum_mismatch"),
        ("binary_sha256", "ripgrep_binary_checksum_mismatch"),
    ),
)
def test_install_accepts_only_the_bytes_the_manifest_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str | None, reason: str | None
) -> None:
    result = install_test_ripgrep(tmp_path, monkeypatch, tamper=tamper)

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
