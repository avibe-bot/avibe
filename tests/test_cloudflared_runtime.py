from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import psutil
import pytest

from core import managed_runtime
from core.cloudflared_runtime import CloudflaredRuntimeManager
from tests.test_remote_access_vibe_cloud import _config
from vibe import remote_access, runtime

# Real fake-cloudflared connectors and their log sinks.
pytestmark = [pytest.mark.reap_processes, pytest.mark.usefixtures("reap_helper_spawns")]

UPSTREAM = "https://github.com/cloudflare/cloudflared/releases/download/2026.10.3"


def _fake_cloudflared(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "cloudflared version {version} (built test)"; exit 0; fi\n'
        "while :; do sleep 0.2; done\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _pin_test_cloudflared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, version: str = "2026.10.0", **overrides
) -> Path:
    """Pin a stand-in raw cloudflared executable in a test manifest the default manager reads."""

    asset = _fake_cloudflared(tmp_path / "release" / version / "cloudflared-test", version)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    entry = {
        "name": asset.name,
        "url": asset.as_uri(),
        "format": "binary",
        "sha256": digest,
        "binary_sha256": digest,
        "size": asset.stat().st_size,
        "bin_path": "cloudflared",
        **overrides,
    }
    manifest = tmp_path / "cloudflared_runtime_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cloudflared_version": version,
                "source": "test",
                "release_tag": version,
                "archives": {managed_runtime.runtime_platform_tag(): entry},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_CLOUDFLARED_MANIFEST_PATH", str(manifest))
    return asset


def test_packaged_manifest_pins_one_release_for_every_platform(tmp_path: Path) -> None:
    manifest = CloudflaredRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)._load_manifest(
        allow_network=False
    )

    assert manifest is not None
    assert manifest.runtime_version == manifest.payload["release_tag"] == "2026.10.3"
    assets = {
        "darwin-arm64": ("cloudflared-darwin-arm64.tgz", "tar.gz", "cloudflared"),
        "darwin-x64": ("cloudflared-darwin-amd64.tgz", "tar.gz", "cloudflared"),
        "linux-arm64": ("cloudflared-linux-arm64", "binary", "cloudflared"),
        "linux-x64": ("cloudflared-linux-amd64", "binary", "cloudflared"),
        "win32-x64": ("cloudflared-windows-amd64.exe", "binary", "cloudflared.exe"),
    }
    assert set(manifest.archives) == set(assets)
    for platform_tag, (name, archive_format, bin_path) in assets.items():
        archive = manifest.archives[platform_tag]
        assert (archive.name, archive.format, archive.bin_path) == (name, archive_format, bin_path)
        assert archive.url == f"{UPSTREAM}/{name}"
        # A raw asset is the executable; a darwin archive's digest is not the binary's.
        assert (archive.sha256 == archive.binary_sha256) == (archive_format == "binary")


def test_raw_binary_asset_installs_verified_at_its_bin_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asset = _pin_test_cloudflared(tmp_path, monkeypatch)

    result = CloudflaredRuntimeManager().ensure()

    assert result["ok"] is True
    binary = CloudflaredRuntimeManager().resolve_binary()
    assert binary is not None and binary.name == "cloudflared"
    assert binary.read_bytes() == asset.read_bytes()
    assert os.access(binary, os.X_OK)


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"binary_sha256": "0" * 64}, "cloudflared_binary_checksum_mismatch"),
        ({"format": "tar.xz"}, "cloudflared_manifest_invalid"),
    ],
)
def test_unverifiable_or_unknown_assets_do_not_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: dict, reason: str
) -> None:
    _pin_test_cloudflared(tmp_path, monkeypatch, **override)

    result = CloudflaredRuntimeManager().ensure()

    assert result["ok"] is False
    assert result["reason"] == reason
    assert CloudflaredRuntimeManager().resolve_binary() is None


def test_windows_arm64_installs_the_x64_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = CloudflaredRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    manifest = manager._load_manifest(allow_network=False)
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "win32-arm64")

    archive = manager._manifest_archive_for_platform(manifest)

    assert archive is not None and archive.name == "cloudflared-windows-amd64.exe"


@pytest.fixture
def remote_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    # No pin until a test sets one: nothing downloads from the network.
    monkeypatch.setenv("VIBE_CLOUDFLARED_MANIFEST_PATH", str(tmp_path / "no-pin.json"))
    config = _config()
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-token"
    config.save()
    monkeypatch.setattr(remote_access, "_PREFERRED_PROTOCOL", None)
    monkeypatch.setattr(remote_access, "_report_runtime_status_async", lambda *args, **kwargs: None)

    def process_command(pid: int) -> str | None:
        # The macOS fallback runs setuid ``ps``, which a sandboxed test cannot exec.
        try:
            return " ".join(psutil.Process(pid).cmdline()) or None
        except psutil.Error:
            return None

    monkeypatch.setattr(runtime, "get_process_command", process_command)
    yield config
    remote_access.stop()


def _start_released_connector(monkeypatch: pytest.MonkeyPatch, binary: Path) -> int:
    """Run a connector from ``binary`` the way a released version resolved it."""

    with monkeypatch.context() as patched:
        patched.setattr(remote_access, "_resolve_binary", lambda config=None: str(binary))
        result = remote_access.start()
    assert result["ok"] is True and result["started"] is True
    return int(result["pid"])


def _recorded_binary() -> str | None:
    return (remote_access._read_state() or {}).get("binary_path")


@pytest.mark.parametrize("released_source", ["unpinned_managed_copy", "homebrew_on_path"])
def test_existing_connector_moves_to_the_pinned_copy(
    remote_home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, released_source: str
) -> None:
    if released_source == "unpinned_managed_copy":
        released = _fake_cloudflared(remote_access._legacy_cloudflared_path(), "2026.8.0")
    else:
        released = _fake_cloudflared(tmp_path / "homebrew" / "bin" / "cloudflared", "2026.3.0")
        monkeypatch.setenv("PATH", f"{released.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    released_pid = _start_released_connector(monkeypatch, released)
    _pin_test_cloudflared(tmp_path, monkeypatch)

    # UI-start reconcile after the upgrade.
    result = remote_access.start()

    pinned = CloudflaredRuntimeManager().resolve_binary()
    assert result["ok"] is True and result["started"] is True
    assert pinned is not None and _recorded_binary() == str(pinned)
    assert remote_access._is_cloudflared_pid(result["pid"])
    assert not remote_access._is_cloudflared_pid(released_pid)
    assert remote_access._legacy_cloudflared_path().exists() is False
    if released_source == "homebrew_on_path":
        assert released.exists()  # never touched


@pytest.mark.parametrize("released_source", ["unpinned_managed_copy", "homebrew_on_path"])
def test_failed_download_keeps_the_running_connector_and_its_binary(
    remote_home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, released_source: str
) -> None:
    if released_source == "unpinned_managed_copy":
        released = _fake_cloudflared(remote_access._legacy_cloudflared_path(), "2026.8.0")
    else:
        released = _fake_cloudflared(tmp_path / "homebrew" / "bin" / "cloudflared", "2026.3.0")
        monkeypatch.setenv("PATH", f"{released.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    released_pid = _start_released_connector(monkeypatch, released)
    _pin_test_cloudflared(tmp_path, monkeypatch, url=(tmp_path / "missing").as_uri())

    result = remote_access.start()

    assert result["ok"] is True and result["started"] is False
    assert remote_access._read_pid() == released_pid
    assert remote_access._is_cloudflared_pid(released_pid)
    assert released.exists()
    # status, settings, recovery, and restart resolve the binary the tracked connector runs.
    assert remote_access._resolve_binary(remote_home) == str(released)
    assert remote_access.status(remote_home)["binary_found"] is True

    remote_access.stop()

    # Without a tracked connector nothing falls back to that binary, and PATH is never searched.
    assert remote_access._resolve_binary(remote_home) is None


def test_failed_download_does_not_start_from_the_unpinned_copy(
    remote_home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _fake_cloudflared(remote_access._legacy_cloudflared_path(), "2026.8.0")
    _pin_test_cloudflared(tmp_path, monkeypatch, url=(tmp_path / "missing").as_uri())

    result = remote_access.start()

    # Same as a fresh install: the install error, and the request lets the monitor retry.
    assert result["ok"] is False and result["error"] == "cloudflared_install_failed"
    assert remote_access._read_pid() is None
    assert remote_access._connector_requested_path().is_file()
    assert legacy.exists()


def test_explicit_cloudflared_path_wins_and_path_is_not_consulted(
    remote_home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    homebrew = _fake_cloudflared(tmp_path / "homebrew" / "bin" / "cloudflared", "2026.3.0")
    monkeypatch.setenv("PATH", f"{homebrew.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    explicit = _fake_cloudflared(tmp_path / "explicit" / "cloudflared", "2026.9.0")

    assert remote_access._resolve_binary(remote_home) is None  # nothing pinned installed, PATH ignored
    remote_home.remote_access.vibe_cloud.cloudflared_path = str(explicit)
    remote_home.save()

    result = remote_access.start()

    assert result["ok"] is True and _recorded_binary() == str(explicit)
    assert remote_access._is_cloudflared_pid(result["pid"])


def test_prepare_installs_the_pin_only_while_remote_access_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibe import cli

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _pin_test_cloudflared(tmp_path, monkeypatch)
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    assert cli._ensure_cloudflared_during_prepare() == {"ok": True, "skipped": True, "reason": "remote_access_disabled"}
    assert CloudflaredRuntimeManager().resolve_binary() is None

    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-token"
    config.save()

    assert cli._ensure_cloudflared_during_prepare()["ok"] is True
    assert CloudflaredRuntimeManager().resolve_binary() is not None


def test_prepare_skips_the_pin_when_an_explicit_cloudflared_path_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibe import cli

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _pin_test_cloudflared(tmp_path, monkeypatch)
    config = _config()
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-token"
    config.remote_access.vibe_cloud.cloudflared_path = str(
        _fake_cloudflared(tmp_path / "explicit" / "cloudflared", "2026.9.0")
    )
    config.save()

    assert cli._ensure_cloudflared_during_prepare() == {
        "ok": True,
        "skipped": True,
        "reason": "explicit_cloudflared_path",
    }
    assert CloudflaredRuntimeManager().resolve_binary() is None


def test_version_probe_runs_without_a_console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core import cloudflared_runtime

    consoles = []

    def isolated_subprocess_kwargs(*, console: bool = True) -> dict:
        consoles.append(console)
        return {}

    monkeypatch.setattr(cloudflared_runtime, "isolated_subprocess_kwargs", isolated_subprocess_kwargs)

    version = cloudflared_runtime._probe_cloudflared_version(_fake_cloudflared(tmp_path / "cloudflared", "2026.10.0"))

    assert version == "2026.10.0"
    assert consoles == [False]


def test_a_new_pin_replaces_an_earlier_verified_install(remote_home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_test_cloudflared(tmp_path, monkeypatch, version="2026.9.0")
    first = remote_access.start()
    earlier = CloudflaredRuntimeManager().resolve_binary()
    assert first["ok"] is True and earlier is not None and _recorded_binary() == str(earlier)

    # The next Avibe release packages a newer pin; UI-start reconcile moves to it.
    _pin_test_cloudflared(tmp_path, monkeypatch, version="2026.10.0")
    result = remote_access.start()

    pinned = CloudflaredRuntimeManager().resolve_binary()
    assert result["ok"] is True and result["started"] is True
    assert pinned is not None and pinned != earlier
    assert "2026.10.0" in str(pinned) and _recorded_binary() == str(pinned)
    assert not remote_access._is_cloudflared_pid(first["pid"])


def test_explicit_legacy_path_through_an_alias_is_never_removed(
    remote_home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _fake_cloudflared(remote_access._legacy_cloudflared_path(), "2026.8.0")
    alias = tmp_path / "vibe_remote_alias"
    alias.symlink_to(legacy.parent.parent, target_is_directory=True)
    remote_home.remote_access.vibe_cloud.cloudflared_path = str(alias / "bin" / legacy.name)
    remote_home.save()

    result = remote_access.start()

    assert result["ok"] is True and result["started"] is True
    assert legacy.exists()
