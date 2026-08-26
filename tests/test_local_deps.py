"""Hermetic tests for the askill local-dependency helpers in vibe/api.py.

The subprocess / path-resolution boundary is monkeypatched, so these run
without askill, npm, or the network — they pin the install command
construction, the idempotency of ``ensure_askill_installed``, and the status
shape.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import urllib.error
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import latest_version_cache
from vibe import api


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self._body


def _fake_avault_archive(
    content: bytes | None = None,
    *,
    member_name: str = "avault",
) -> bytes:
    if content is None:
        content = f"#!/bin/sh\necho avault {api.AVAULT_VERSION}\n".encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        info = tarfile.TarInfo(member_name)
        info.size = len(content)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(content))
    return raw.getvalue()


def _installable_avault_release(
    monkeypatch,
    *,
    target: str = "macos-arm64",
    sha256: str | None = None,
    content: bytes | None = None,
):
    if content is None:
        content = f"#!/bin/sh\necho avault {api.AVAULT_VERSION}\n".encode()
    member_name = api._avault_binary_name_for_target(target)
    archive = _fake_avault_archive(content=content, member_name=member_name)
    digest = sha256 or hashlib.sha256(archive).hexdigest()
    manifest = {
        "schema_version": 1,
        "versions": {
            api.AVAULT_VERSION: {
                target: {
                    "asset": f"avault-{api.AVAULT_VERSION}-{target}.tar.gz",
                    "sha256": digest,
                }
            }
        },
    }
    calls: list[str] = []

    def fake_urlopen(request, timeout=30):
        url = request.full_url
        calls.append(url)
        if url.endswith("/manifest.json"):
            return _FakeHTTPResponse(json.dumps(manifest).encode("utf-8"))
        if url.endswith(f"/avault-{api.AVAULT_VERSION}-{target}.tar.gz"):
            return _FakeHTTPResponse(archive)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    def fake_candidate_cli_paths(binary: str):
        expanded = api.Path(api.os.path.expanduser(binary))
        has_path_separator = api.os.sep in binary or (api.os.altsep is not None and api.os.altsep in binary)
        if expanded.is_absolute() or has_path_separator:
            return [expanded]
        if binary == "avault":
            return [api._avault_managed_bin_path(target)]
        return []

    monkeypatch.setattr(api.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(api, "_candidate_cli_paths", fake_candidate_cli_paths)
    return calls, member_name


def test_install_askill_uses_official_curl_installer(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: f"/usr/bin/{b}" if b in {"curl", "bash"} else None)

    def fake_run(name, cmd, _trunc, *, mode="install", env=None):
        captured.update(name=name, cmd=cmd, mode=mode)
        return {"ok": True, "path": "/usr/local/bin/askill", "output": ""}

    monkeypatch.setattr(api, "_run_install_command", fake_run)
    out = api.install_askill()
    assert out["ok"]
    assert captured["name"] == "askill"
    assert captured["cmd"][:2] == ["bash", "-c"]
    assert "https://askill.sh | sh" in captured["cmd"][2]
    assert "--retry 2" in captured["cmd"][2]
    assert "--retry-all-errors" not in captured["cmd"][2]


def test_curl_installer_command_uses_pipe_safe_retry_flags(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_log = tmp_path / "curl.log"
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {curl_log!s}\n"
        "printf '#!/bin/sh\\nexit 0\\n'\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])

    result = subprocess.run(
        api._curl_installer_command("https://example.test/install.sh", "sh"),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    calls = curl_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert "--retry 2" in calls[0]
    assert "--retry-all-errors" not in calls[0]


def test_avault_download_retries_transient_network_failure(monkeypatch):
    attempts = 0

    def opener(_request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError(ConnectionResetError("reset"))
        return _FakeHTTPResponse(b"manifest")

    monkeypatch.setattr(api.urllib.request, "urlopen", opener)
    monkeypatch.setattr("core.dependency_network.time.sleep", lambda _delay: None)

    result = api._download_avault_release_file("https://example.test/manifest.json")

    assert result == b"manifest"
    assert attempts == 2


def test_askill_install_command_does_not_persist_agent_cli_path(monkeypatch):
    config_loads = []

    class FakePopen:
        returncode = 0

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, timeout=None):
            return "installed", ""

    monkeypatch.setattr(api.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/askill" if b == "askill" else None)
    monkeypatch.setattr(api, "load_config", lambda: config_loads.append(True) or pytest.fail("askill should not load V2Config"))

    out = api._run_install_command("askill", ["bash", "-c", "true"], lambda value: value, mode="install")

    assert out["ok"] is True
    assert out["path"] == "/usr/local/bin/askill"
    assert config_loads == []


def test_install_askill_unsupported_without_curl(monkeypatch):
    # No curl/bash (e.g. Windows): no broken npm fallback — a clear manual
    # message pointing at askill.sh, and _run_install_command is never invoked.
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: None)
    monkeypatch.setattr(api, "_run_install_command", lambda *a, **k: pytest.fail("should not install"))
    out = api.install_askill()
    assert out["ok"] is False
    assert "askill.sh" in out["message"]


def test_install_askill_unsupported_on_windows_even_with_tools(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr(api, "resolve_cli_path", lambda binary: f"C:/{binary}.exe")
    monkeypatch.setattr(api, "_run_install_command", lambda *a, **k: pytest.fail("should not install"))

    out = api.install_askill()

    assert out["ok"] is False
    assert "askill.sh" in out["message"]


def test_ensure_askill_idempotent_when_present(monkeypatch):
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/askill")
    flag = {"installed": False}
    monkeypatch.setattr(api, "install_askill", lambda: flag.__setitem__("installed", True) or {"ok": True})
    out = api.ensure_askill_installed()
    assert out == {"ok": True, "installed": True, "changed": False, "path": "/usr/local/bin/askill"}
    assert flag["installed"] is False  # never installed when already present


def test_ensure_askill_installs_when_missing(monkeypatch):
    # Missing on the first check, resolvable after install.
    seen = {"n": 0}

    def fake_resolve(_b):
        seen["n"] += 1
        return None if seen["n"] == 1 else "/x/askill"

    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)
    monkeypatch.setattr(api, "install_askill", lambda: {"ok": True})
    out = api.ensure_askill_installed()
    assert out["ok"] and out["installed"] and out["changed"] and out["path"] == "/x/askill"


def test_ensure_askill_install_not_discoverable_is_failure(monkeypatch):
    # Installer exits 0 but the binary never resolves on the service PATH —
    # must NOT report success, or the UI claims installed while skills 404.
    monkeypatch.setattr(api, "resolve_cli_path", lambda _b: None)
    monkeypatch.setattr(api, "install_askill", lambda: {"ok": True})
    out = api.ensure_askill_installed()
    assert out["ok"] is False and out["installed"] is False and out["path"] is None


def test_ensure_askill_force_reinstalls_even_when_present(monkeypatch):
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/askill")
    flag = {"installed": False}
    monkeypatch.setattr(api, "install_askill", lambda: flag.__setitem__("installed", True) or {"ok": True})
    api.ensure_askill_installed(force=True)
    assert flag["installed"] is True


def test_ensure_askill_skips_when_install_already_running():
    assert api._ASKILL_INSTALL_LOCK.acquire(blocking=False) is True
    try:
        out = api.ensure_askill_installed(force=True)
    finally:
        api._ASKILL_INSTALL_LOCK.release()

    assert out["ok"] is False
    assert out["skipped"] is True
    assert out["reason"] == "askill_install_already_running"
    assert "already running" in out["message"]


def test_askill_status_missing(monkeypatch):
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: None)
    s = api.askill_status()
    assert s["installed"] is False and s["status"] == "missing" and s["version"] is None


def test_askill_status_present_parses_version(monkeypatch):
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/x/askill")
    monkeypatch.setattr(api, "_command_env_for", lambda p: {})
    monkeypatch.setattr(api, "isolated_subprocess_kwargs", lambda: {})

    class _R:
        returncode = 0
        stdout = "askill 0.1.13\n"
        stderr = ""

    monkeypatch.setattr(api.subprocess, "run", lambda *a, **k: _R())
    s = api.askill_status()
    assert s["installed"] and s["version"] == "0.1.13" and s["status"] == "ready"


def test_install_avault_uses_existing_configured_binary(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "/opt/avault/bin/avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/opt/avault/bin/avault" if b == "/opt/avault/bin/avault" else None)
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: api.AVAULT_P2_MIN_VERSION)

    out = api.install_avault()

    assert out["ok"] is True
    assert out["path"] == "/opt/avault/bin/avault"
    assert out["version"] == api.AVAULT_P2_MIN_VERSION


def test_install_avault_existing_binary_below_p2_is_accepted_for_standard_surface(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "/opt/avault/bin/avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/opt/avault/bin/avault" if b == "/opt/avault/bin/avault" else None)
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: "0.1.1")
    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: pytest.fail("should not download old avault"))

    out = api.install_avault()

    assert out["ok"] is True
    assert out["path"] == "/opt/avault/bin/avault"
    assert out["version"] == "0.1.1"


def test_install_avault_unsupported_platform_is_clear_failure(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda _b: None)
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")
    monkeypatch.setattr("platform.machine", lambda: "riscv64")
    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: pytest.fail("should not download"))

    out = api.install_avault()

    assert out["ok"] is False
    assert "no avault build for FreeBSD-riscv64" in out["message"]


def test_install_avault_force_keeps_existing_binary_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "/opt/avault/bin/avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/opt/avault/bin/avault" if b == "/opt/avault/bin/avault" else None)
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: api.AVAULT_P2_MIN_VERSION)
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")
    monkeypatch.setattr("platform.machine", lambda: "riscv64")
    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: pytest.fail("should not download"))

    out = api.install_avault(force=True)

    assert out["ok"] is True
    assert out["path"] == "/opt/avault/bin/avault"
    assert out["version"] == api.AVAULT_P2_MIN_VERSION


def test_install_avault_force_keeps_old_existing_binary_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "/opt/avault/bin/avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/opt/avault/bin/avault" if b == "/opt/avault/bin/avault" else None)
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: "0.1.1")
    monkeypatch.setattr("platform.system", lambda: "FreeBSD")
    monkeypatch.setattr("platform.machine", lambda: "riscv64")
    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: pytest.fail("should not download"))

    out = api.install_avault(force=True)

    assert out["ok"] is True
    assert out["path"] == "/opt/avault/bin/avault"
    assert out["version"] == "0.1.1"


@pytest.mark.parametrize(
    ("system", "machine", "target"),
    [
        ("Darwin", "arm64", "macos-arm64"),
        ("Darwin", "x86_64", "macos-x64"),
        ("Linux", "x86_64", "linux-x64"),
        ("Linux", "amd64", "linux-x64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Linux", "arm64", "linux-arm64"),
        ("Windows", "AMD64", "windows-x64"),
        ("Windows", "x86_64", "windows-x64"),
        ("Windows", "ARM64", "windows-arm64"),
    ],
)
def test_avault_target_detects_supported_platforms(monkeypatch, system, machine, target):
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)

    assert api._avault_target() == (target, f"{system}-{machine}")


def test_windows_candidate_paths_include_managed_exe(monkeypatch):
    candidates = api._windows_executable_candidates([api.Path.home() / ".local" / "bin" / "avault"])

    assert api.Path.home() / ".local" / "bin" / "avault.exe" in candidates


@pytest.mark.parametrize(
    ("system", "machine", "target"),
    [
        ("Darwin", "arm64", "macos-arm64"),
        ("Darwin", "x86_64", "macos-x64"),
        ("Linux", "x86_64", "linux-x64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Windows", "AMD64", "windows-x64"),
        ("Windows", "ARM64", "windows-arm64"),
    ],
)
def test_install_avault_downloads_manifest_verifies_and_installs(monkeypatch, system, machine, target):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_p2", lambda: True)
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    calls, member_name = _installable_avault_release(monkeypatch, target=target)

    out = api.install_avault()

    installed = api.Path.home() / ".local" / "bin" / member_name
    assert out["ok"] is True
    assert out["path"] == str(installed)
    assert installed.exists()
    if not target.startswith("windows-"):
        assert installed.stat().st_mode & 0o777 == 0o755
    if target.startswith("windows-"):
        assert api.V2Config.load().agents.avault.cli_path == str(installed)
    else:
        assert api.resolve_cli_path("avault") == str(installed)
    assert calls == [
        f"https://github.com/avibe-bot/avault/releases/download/v{api.AVAULT_VERSION}/manifest.json",
        f"https://github.com/avibe-bot/avault/releases/download/v{api.AVAULT_VERSION}/avault-{api.AVAULT_VERSION}-{target}.tar.gz",
    ]


def test_install_avault_checksum_mismatch_installs_nothing(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_p2", lambda: True)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    _installable_avault_release(monkeypatch, target="macos-arm64", sha256="0" * 64)

    out = api.install_avault()

    installed = api.Path.home() / ".local" / "bin" / "avault"
    assert out["ok"] is False
    assert "checksum" in out["message"]
    assert not installed.exists()


def test_install_avault_is_idempotent_when_present(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: api.AVAULT_P2_MIN_VERSION)
    installed = api.Path.home() / ".local" / "bin" / "avault"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text("#!/bin/sh\n", encoding="utf-8")
    installed.chmod(0o755)
    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: pytest.fail("should not download"))

    out = api.install_avault()

    assert out["ok"] is True
    assert out["path"] == str(installed)


def test_install_avault_force_redownloads_when_present(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_p2", lambda: True)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    installed = api.Path.home() / ".local" / "bin" / "avault"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text("old\n", encoding="utf-8")
    installed.chmod(0o755)
    calls, _member_name = _installable_avault_release(monkeypatch, target="macos-arm64")

    out = api.install_avault(force=True)

    assert out["ok"] is True
    assert len(calls) == 2
    assert installed.read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_install_avault_force_resets_resident_agent_after_binary_change(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    manager = Mock()
    manager.socket_path = api.Path.home() / ".avibe" / "run" / "avault.sock"
    monkeypatch.setattr(api, "_AVAULT_AGENT_MANAGER", manager)
    quarantined: list[api.Path] = []
    monkeypatch.setattr(api, "_quarantine_resident_agent_socket", lambda path: quarantined.append(path))
    _installable_avault_release(monkeypatch, target="macos-arm64")

    out = api.install_avault(force=True)

    assert out["ok"] is True
    manager.reset.assert_called_once()
    assert quarantined == [manager.socket_path]


def test_ensure_avault_force_uses_managed_binary_after_install(monkeypatch):
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_p2", lambda: True)
    configured = api.Path.home() / "custom" / "avault"
    configured.parent.mkdir(parents=True, exist_ok=True)
    configured.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    configured.chmod(0o755)
    cfg = api.save_config({})
    cfg.agents.avault.cli_path = str(configured)
    cfg.save()
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    _installable_avault_release(monkeypatch, target="macos-arm64")

    out = api.ensure_avault_installed(force=True)

    installed = api.Path.home() / ".local" / "bin" / "avault"
    assert out["ok"] is True
    assert out["path"] == str(installed)
    assert api.V2Config.load().agents.avault.cli_path == str(installed)
    assert api._resolve_avault_cli_path() == str(installed)


def test_avault_resolves_path_fallback_when_configured_path_missing(monkeypatch):
    seen = []

    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "/missing/avault")

    def fake_resolve(binary):
        seen.append(binary)
        return "/usr/local/bin/avault" if binary == "avault" else None

    monkeypatch.setattr(api, "resolve_cli_path", fake_resolve)

    assert api._resolve_avault_cli_path() == "/usr/local/bin/avault"
    assert seen == ["/missing/avault", "avault"]


def test_ensure_avault_idempotent_when_present(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/avault")
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: api._avault_ready_min_version())
    flag = {"installed": False}
    monkeypatch.setattr(api, "install_avault", lambda force=False: flag.__setitem__("installed", True) or {"ok": True})

    out = api.ensure_avault_installed()

    assert out == {
        "ok": True,
        "installed": True,
        "changed": False,
        "path": "/usr/local/bin/avault",
        "version": api._avault_ready_min_version(),
    }
    assert flag["installed"] is False


def test_ensure_avault_force_rechecks_existing_binary(monkeypatch):
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_p2", lambda: True)
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/avault")
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: api.AVAULT_P2_MIN_VERSION)
    flag = {"installed": False}
    monkeypatch.setattr(api, "install_avault", lambda force=False: flag.__setitem__("installed", True) or {"ok": True})

    out = api.ensure_avault_installed(force=True)

    assert flag["installed"] is True
    assert out["ok"] is True
    assert out["installed"] is True
    assert out["changed"] is True


def test_ensure_avault_force_does_not_downgrade_compatible_binary_when_pin_is_old(monkeypatch):
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_ready_minimum", lambda: False)
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/avault")
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: api._avault_ready_min_version())
    monkeypatch.setattr(api, "install_avault", lambda force=False: pytest.fail("should not downgrade avault"))

    out = api.ensure_avault_installed(force=True)

    assert out == {
        "ok": True,
        "installed": True,
        "changed": False,
        "path": "/usr/local/bin/avault",
        "version": api._avault_ready_min_version(),
    }


def test_ensure_avault_force_does_not_downgrade_newer_binary(monkeypatch):
    # A user/custom avault newer than the managed pin must survive `force` prepare,
    # even though the managed pin now satisfies the P2 gate (Codex #686 P2 finding).
    newer = "9.9.9"
    assert api._version_at_least(newer, api.AVAULT_VERSION)
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_p2", lambda: True)
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/avault")
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: newer)
    monkeypatch.setattr(api, "install_avault", lambda force=False: pytest.fail("should not downgrade a newer avault"))

    out = api.ensure_avault_installed(force=True)

    assert out == {
        "ok": True,
        "installed": True,
        "changed": False,
        "path": "/usr/local/bin/avault",
        "version": newer,
    }


def test_ensure_avault_upgrades_existing_binary_below_grant_delivery_minimum(monkeypatch):
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_ready_minimum", lambda: True)
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/avault")
    seen_force: list[bool] = []
    versions = iter([api.AVAULT_P2_MIN_VERSION, api.AVAULT_GRANT_DELIVERY_MIN_VERSION])
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: next(versions))

    def fake_install(force=False):
        seen_force.append(force)
        return {"ok": True, "path": "/usr/local/bin/avault"}

    monkeypatch.setattr(api, "install_avault", fake_install)

    out = api.ensure_avault_installed()

    assert seen_force == [True]
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["version"] == api.AVAULT_GRANT_DELIVERY_MIN_VERSION


def test_ensure_avault_keeps_existing_standard_release_when_ready_pin_unavailable(monkeypatch):
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_ready_minimum", lambda: False)
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/avault")
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: "0.1.1")
    monkeypatch.setattr(api, "install_avault", lambda force=False: pytest.fail("should not install old avault"))

    out = api.ensure_avault_installed()

    assert out["ok"] is True
    assert out["installed"] is True
    assert out["changed"] is False
    assert out["path"] == "/usr/local/bin/avault"
    assert out["version"] == "0.1.1"


def test_ensure_avault_force_reports_manual_upgrade_when_pin_is_below_ready_minimum(monkeypatch):
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_ready_minimum", lambda: False)
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/usr/local/bin/avault")
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: "0.1.1")
    monkeypatch.setattr(api, "install_avault", lambda force=False: pytest.fail("should not reinstall old avault"))

    out = api.ensure_avault_installed(force=True)

    assert out["ok"] is False
    assert out["installed"] is True
    assert out["changed"] is False
    assert out["path"] == "/usr/local/bin/avault"
    assert out["version"] == "0.1.1"
    assert out["status"] == "upgrade_required"
    assert out["reason"] == "avault_p2_release_unavailable"


def test_ensure_avault_installs_pinned_release_even_when_ready_pin_unavailable(monkeypatch):
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_ready_minimum", lambda: False)
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    _installable_avault_release(monkeypatch, target="macos-arm64", content=b"#!/bin/sh\necho avault 0.1.1\n")
    monkeypatch.setattr(api, "_probe_avault_version", lambda path: "0.1.1" if path else None)

    out = api.ensure_avault_installed()

    installed = api.Path.home() / ".local" / "bin" / "avault"
    assert out["ok"] is True
    assert out["installed"] is True
    assert out["changed"] is True
    assert out["path"] == str(installed)
    assert out["version"] == "0.1.1"
    assert out["status"] == "upgrade_required"


def test_avault_status_missing(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: None)
    s = api.avault_status()
    assert s["id"] == "avault"
    assert s["installed"] is False and s["status"] == "missing" and s["version"] is None


def test_avault_status_present_parses_version(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/x/avault")
    monkeypatch.setattr(api, "_command_env_for", lambda p: {})
    monkeypatch.setattr(api, "isolated_subprocess_kwargs", lambda: {})

    class _R:
        returncode = 0
        stdout = f"avault {api._avault_ready_min_version()}\n"
        stderr = ""

    monkeypatch.setattr(api.subprocess, "run", lambda *a, **k: _R())
    s = api.avault_status()
    assert s["installed"] and s["version"] == api._avault_ready_min_version() and s["status"] == "ready"


def test_avault_status_marks_p2_only_version_upgrade_required(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/x/avault")
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: api.AVAULT_P2_MIN_VERSION)

    s = api.avault_status()

    assert s["installed"] is True
    assert s["version"] == api.AVAULT_P2_MIN_VERSION
    assert s["status"] == "upgrade_required"


def test_avault_status_marks_old_version_upgrade_required(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: "/x/avault")
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: "0.1.1")

    s = api.avault_status()

    assert s["installed"] is True
    assert s["version"] == "0.1.1"
    assert s["status"] == "upgrade_required"


def test_askill_update_status_compares_latest(monkeypatch):
    monkeypatch.setattr(
        api,
        "askill_status",
        lambda: {"id": "askill", "installed": True, "version": "0.1.13", "status": "ready", "path": "/x"},
    )
    monkeypatch.setattr(api, "_cached_latest_askill", lambda: "0.1.14")

    s = api.askill_update_status()

    assert s["latest_version"] == "0.1.14"
    assert s["has_update"] is True
    assert s["auto_update"] is True


def test_reconcile_askill_auto_update_installs_when_missing(monkeypatch):
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {"id": "askill", "installed": False, "version": None, "status": "missing", "latest_version": "0.1.14"},
    )
    calls = []

    def fake_ensure(force=False):
        calls.append(force)
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", fake_ensure)

    out = api.reconcile_askill_auto_update()

    assert calls == [False]
    assert out["ok"] is True and out["action"] == "install"


def test_reconcile_askill_auto_update_refreshes_when_newer(monkeypatch):
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {
            "id": "askill",
            "installed": True,
            "version": "0.1.13",
            "status": "ready",
            "latest_version": "0.1.14",
            "has_update": True,
        },
    )
    calls = []

    def fake_ensure(force=False):
        calls.append(force)
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", fake_ensure)

    out = api.reconcile_askill_auto_update()

    assert calls == [True]
    assert out["ok"] is True
    assert out["action"] == "update"
    assert out["from_version"] == "0.1.13"
    assert out["latest_version"] == "0.1.14"


def test_reconcile_askill_auto_update_refreshes_when_current_version_unknown(monkeypatch):
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {
            "id": "askill",
            "installed": True,
            "version": None,
            "status": "unknown",
            "latest_version": "0.1.14",
            "has_update": False,
        },
    )
    calls = []

    def fake_ensure(force=False):
        calls.append(force)
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", fake_ensure)

    out = api.reconcile_askill_auto_update()

    assert calls == [True]
    assert out["ok"] is True
    assert out["action"] == "refresh_unknown_version"
    assert out["latest_version"] == "0.1.14"


def test_reconcile_askill_auto_update_skips_when_disabled(monkeypatch):
    monkeypatch.setenv("VIBE_ASKILL_AUTO_UPDATE", "0")
    monkeypatch.setattr(api, "askill_update_status", lambda **_: pytest.fail("should not probe"))

    out = api.reconcile_askill_auto_update()

    assert out == {"ok": True, "skipped": True, "reason": "askill_auto_update_disabled"}


def test_refresh_askill_if_stale_does_not_run_the_installer_when_current(monkeypatch):
    # The askill.sh installer re-downloads the CLI whenever it runs, so the
    # shared currency owner must answer "already current" without invoking it.
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {
            "id": "askill",
            "installed": True,
            "version": "0.1.14",
            "status": "ready",
            "path": "/x/askill",
            "latest_version": "0.1.14",
            "has_update": False,
        },
    )
    monkeypatch.setattr(api, "ensure_askill_installed", lambda force=False: pytest.fail("should not install"))

    out = api.refresh_askill_if_stale()

    assert out["ok"] is True
    assert out["reason"] == "up_to_date"
    assert "action" not in out


def test_a_second_prepare_process_reuses_the_persisted_askill_latest(monkeypatch):
    # The waste this closes: ``vibe runtime prepare`` is a fresh process on every
    # install, upgrade, regression sync, and tenant update, and each one used to
    # spend a GitHub request re-learning askill's newest release. That request
    # comes out of the unauthenticated 60/hour/IP budget, and exhausting it makes
    # the latest lookup fail, which makes prepare reinstall askill outright.
    monkeypatch.setattr(
        api,
        "askill_status",
        lambda: {"id": "askill", "installed": True, "version": "0.1.14", "status": "ready"},
    )
    monkeypatch.setattr(api, "ensure_askill_installed", lambda force=False: pytest.fail("should not install"))
    probes = []
    monkeypatch.setattr(
        api,
        "_fetch_latest_askill_version",
        lambda: probes.append(1) or "0.1.14",
    )

    assert api.refresh_askill_if_stale()["reason"] == "up_to_date"
    latest_version_cache._MEMORY.clear()  # noqa: SLF001 - stand in for a new process
    assert api.refresh_askill_if_stale()["reason"] == "up_to_date"

    assert len(probes) == 1


def test_installing_askill_keeps_the_persisted_latest_for_the_next_process(monkeypatch):
    """An install is the one moment prepare runs most, and must not cost a probe.

    Installing 0.1.14 does not change the fact that 0.1.14 is what askill
    publishes, so the entry that justified the install is exactly what the next
    process needs: it compares a freshly measured local version against it and
    concludes ``up_to_date``. Retiring it here — the reflex the in-memory cache
    this replaced had — would send every post-update ``runtime prepare`` back to
    GitHub for a string already on disk.
    """

    installed = {"version": "0.1.13"}
    monkeypatch.setattr(
        api,
        "askill_status",
        lambda: {"id": "askill", "installed": True, "version": installed["version"], "status": "ready"},
    )

    def _install(force=False):
        installed["version"] = "0.1.14"
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", _install)
    probes = []
    monkeypatch.setattr(
        api,
        "_fetch_latest_askill_version",
        lambda: probes.append(1) or "0.1.14",
    )

    assert api.refresh_askill_if_stale()["action"] == "update"
    latest_version_cache._MEMORY.clear()  # noqa: SLF001 - stand in for a new process

    assert api.refresh_askill_if_stale()["reason"] == "up_to_date"
    assert len(probes) == 1


def test_refresh_askill_if_stale_ignores_the_auto_update_gate(monkeypatch):
    # ``VIBE_ASKILL_AUTO_UPDATE`` disables the update-checker cadence, not the
    # lifecycle refresh that ``vibe runtime prepare`` performs; keeping the gate
    # in the cadence wrapper is what lets both callers share one decision.
    monkeypatch.setenv("VIBE_ASKILL_AUTO_UPDATE", "0")
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {
            "id": "askill",
            "installed": True,
            "version": "0.1.13",
            "status": "ready",
            "latest_version": "0.1.14",
            "has_update": True,
        },
    )
    calls = []
    monkeypatch.setattr(
        api,
        "ensure_askill_installed",
        lambda force=False: calls.append(force) or {"ok": True, "installed": True, "changed": True, "path": "/x/askill"},
    )

    out = api.refresh_askill_if_stale()

    assert calls == [True]
    assert out["action"] == "update"


def test_refresh_avault_if_stale_does_not_force_when_the_pin_is_satisfied(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda _b: "/usr/local/bin/avault")
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: api.AVAULT_VERSION)
    monkeypatch.setattr(api, "install_avault", lambda force=False: pytest.fail("should not reinstall the pinned release"))

    out = api.refresh_avault_if_stale()

    assert out["ok"] is True
    assert out["changed"] is False
    assert out["version"] == api.AVAULT_VERSION


def test_refresh_avault_if_stale_upgrades_a_binary_below_the_pin(monkeypatch):
    # Skipping the install is conditional on the pin, not unconditional: prepare
    # still has to raise a stale managed binary on upgrade.
    stale = api.AVAULT_P2_MIN_VERSION
    assert api._version_at_least(api.AVAULT_VERSION, stale)
    state = {"version": stale}
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda _b: "/usr/local/bin/avault")
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: state["version"])
    calls = []

    def _install(force=False):
        calls.append(force)
        state["version"] = api.AVAULT_VERSION
        return {"ok": True}

    monkeypatch.setattr(api, "install_avault", _install)

    out = api.refresh_avault_if_stale()

    assert calls == [True]
    assert out["ok"] is True
    assert out["version"] == api.AVAULT_VERSION


def test_refresh_avault_if_stale_installs_when_missing(monkeypatch):
    state = {"path": None}
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda _b: state["path"])
    monkeypatch.setattr(api, "_probe_avault_version", lambda path: api.AVAULT_VERSION if path else None)
    calls = []

    def _install(force=False):
        calls.append(force)
        state["path"] = "/usr/local/bin/avault"
        return {"ok": True}

    monkeypatch.setattr(api, "install_avault", _install)

    out = api.refresh_avault_if_stale()

    assert calls == [True]
    assert out["ok"] is True
    assert out["installed"] is True
    assert out["version"] == api.AVAULT_VERSION


def _stub_avault_install_state(monkeypatch, *, version, ready_floor):
    """Answer every avault code path from one fake on-disk install."""
    state = {"version": version, "installs": 0}
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda _b: "/usr/local/bin/avault" if state["version"] else None)
    monkeypatch.setattr(api, "_probe_avault_version", lambda path: state["version"] if path else None)
    monkeypatch.setattr(api, "_avault_ready_min_version", lambda: ready_floor)
    monkeypatch.setattr(
        api,
        "_managed_avault_release_satisfies_ready_minimum",
        lambda: api._version_at_least(api.AVAULT_VERSION, ready_floor),
    )

    def _install(force=False):
        state["installs"] += 1
        state["version"] = api.AVAULT_VERSION
        return {"ok": True, "changed": True}

    monkeypatch.setattr(api, "install_avault", _install)
    return state


@pytest.mark.parametrize(
    "version, ready_floor",
    [
        (None, api.AVAULT_VERSION),
        ("0.1.1", api.AVAULT_VERSION),
        (api.AVAULT_VERSION, api.AVAULT_VERSION),
        ("99.0.0", api.AVAULT_VERSION),
        (api.AVAULT_VERSION, "99.0.0"),
    ],
)
def test_refresh_avault_if_stale_never_answers_healthier_than_forcing(monkeypatch, version, ready_floor):
    # The property the whole change rests on: skipping a redundant download may
    # not change the verdict. Whatever ``ensure_avault_installed(force=True)``
    # concludes about an install state, the currency path must conclude the same
    # thing — it may only skip the reinstall. Being at the pin is not the whole
    # of being ready: when the readiness floor is raised ahead of the published
    # pin, a binary equal to the pin is still ``upgrade_required``, and only the
    # forced path used to say so.
    forced_state = _stub_avault_install_state(monkeypatch, version=version, ready_floor=ready_floor)
    forced = api.ensure_avault_installed(force=True)

    stale_state = _stub_avault_install_state(monkeypatch, version=version, ready_floor=ready_floor)
    stale = api.refresh_avault_if_stale()

    assert stale["ok"] == forced["ok"]
    assert stale.get("status") == forced.get("status")
    assert stale.get("reason") == forced.get("reason")
    assert stale_state["installs"] <= forced_state["installs"]


def test_refresh_askill_if_stale_repairs_an_unreadable_binary_without_the_latest_probe(monkeypatch):
    # A binary that cannot report its version is broken, not current, and that
    # verdict must not depend on the upstream probe answering: prepare used to
    # force this install unconditionally, so gating the repair on a reachable
    # latest would report a broken askill as ready during a network blip.
    monkeypatch.setattr(
        api,
        "askill_status",
        lambda: {"id": "askill", "installed": True, "version": None, "status": "unknown", "path": "/x/askill"},
    )
    monkeypatch.setattr(api, "_cached_latest_askill", lambda: None)
    calls = []
    monkeypatch.setattr(
        api,
        "ensure_askill_installed",
        lambda force=False: calls.append(force) or {"ok": True, "installed": True, "changed": True, "path": "/x/askill"},
    )

    out = api.refresh_askill_if_stale()

    assert calls == [True]
    assert out["action"] == "refresh_unknown_version"


# Strings a version probe can produce that no comparison can order: absent,
# empty, a development build, a git description, a partial number. The point is
# not the list — it is that each one makes `_compare_versions` answer False, the
# same answer it gives for "already newest", which is how "cannot tell" used to
# be read as "current".
UNORDERABLE_VERSIONS = [None, "", "dev", "unknown", "askill", "g1a2b3c", "0.1.x"]


@pytest.mark.parametrize("version", UNORDERABLE_VERSIONS)
def test_askill_status_calls_an_unorderable_version_unknown(monkeypatch, version):
    # One field owns the fact, so prepare and the Dependencies page cannot
    # disagree about whether a binary reporting `dev` is healthy.
    monkeypatch.setattr(
        api,
        "askill_status",
        lambda: {"id": "askill", "installed": True, "version": version, "status": "ready", "path": "/x/askill"},
    )
    monkeypatch.setattr(api, "_cached_latest_askill", lambda: "0.1.14")

    assert api.askill_update_status()["status"] == "unknown"


@pytest.mark.parametrize("version", UNORDERABLE_VERSIONS)
def test_refresh_askill_if_stale_repairs_an_unorderable_local_version(monkeypatch, version):
    # `up_to_date` must be an affirmative verdict, never the branch everything
    # unrecognised falls into. A version that cannot be ordered against the
    # published one means unknown, and unknown gets the repair the forced path
    # would have performed — asserted over the shapes a probe can produce rather
    # than over the one the review happened to name.
    monkeypatch.setattr(
        api,
        "askill_status",
        lambda: {"id": "askill", "installed": True, "version": version, "status": "ready", "path": "/x/askill"},
    )
    monkeypatch.setattr(api, "_cached_latest_askill", lambda: "0.1.14")
    calls = []
    monkeypatch.setattr(
        api,
        "ensure_askill_installed",
        lambda force=False: calls.append(force) or {"ok": True, "installed": True, "changed": True},
    )

    out = api.refresh_askill_if_stale()

    assert calls == [True], f"{version!r} is not a version we can trust as current"
    assert out["action"] == "refresh_unknown_version"


@pytest.mark.parametrize("latest", UNORDERABLE_VERSIONS)
def test_refresh_askill_if_stale_needs_an_orderable_latest_to_claim_currency(monkeypatch, latest):
    # The same rule on the upstream side: with nothing orderable to compare
    # against, staleness is undecided. The owner reports that instead of
    # currency, and each caller decides (prepare installs, the cadence skips).
    monkeypatch.setattr(
        api,
        "askill_status",
        lambda: {"id": "askill", "installed": True, "version": "0.1.14", "status": "ready", "path": "/x/askill"},
    )
    monkeypatch.setattr(api, "_cached_latest_askill", lambda: latest)
    monkeypatch.setattr(api, "ensure_askill_installed", lambda force=False: pytest.fail("the owner decides, it does not install here"))

    out = api.refresh_askill_if_stale()

    assert out["reason"] == "latest_unavailable"


def test_dependencies_status_shape(monkeypatch):
    monkeypatch.setattr(
        api.V2Config,
        "load",
        classmethod(lambda _cls: SimpleNamespace(memory=SimpleNamespace(enabled=True))),
    )
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {
            "id": "askill",
            "installed": True,
            "version": "0.1.13",
            "latest_version": None,
            "has_update": False,
            "status": "ready",
            "path": "/x",
        },
    )
    monkeypatch.setattr(
        api,
        "avault_status",
        lambda: {"id": "avault", "installed": True, "version": "0.0.1", "status": "ready", "path": "/x/avault"},
    )
    import core.show_runtime as srt_mod

    class _Mgr:
        def status(self):
            return {
                "install": {"state": "installed", "runtime_version": "1.4.0", "matches_manifest": True},
                "manifest": {"runtime_version": "1.4.0"},
                "node_available": True,
                "node_version": "20.11",
            }

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())
    import avibe_memory.artifact as memory_artifact

    class _MemoryMgr:
        def status(self):
            return {
                "installed": False,
                "version": None,
                "selected_version": "1.2.3",
                "matches_manifest": None,
                "status": "missing",
                "manifest": {"everos_version": "1.2.3", "release_state": "unavailable"},
                "reason": "memory_runtime_unpublished",
            }

    monkeypatch.setattr(memory_artifact, "get_memory_artifact_manager", lambda: _MemoryMgr())
    out = api.dependencies_status()
    assert out["ok"]
    by = {d["id"]: d for d in out["deps"]}
    assert list(by) == ["askill", "avault", "show-runtime", "memory-runtime", "tmux", "node"]
    assert "tmux" in by and by["tmux"]["required"] is False  # tmux is the optional terminal backend
    assert by["askill"]["status"] == "ready" and by["askill"]["version"] == "0.1.13" and by["askill"]["required"]
    assert by["askill"]["latest_version"] is None and by["askill"]["has_update"] is False
    assert by["avault"]["status"] == "ready" and by["avault"]["version"] == "0.0.1" and by["avault"]["required"]
    assert by["avault"]["latest_version"] is None and by["avault"]["has_update"] is False
    assert by["show-runtime"]["installed"] and by["show-runtime"]["version"] == "1.4.0"
    assert by["show-runtime"]["latest_version"] == "1.4.0"
    assert by["show-runtime"]["has_update"] is False
    assert by["memory-runtime"] == {
        "id": "memory-runtime",
        "kind": "runtime",
        "required": True,
        "installed": False,
        "version": None,
        "latest_version": "1.2.3",
        "has_update": False,
        "status": "missing",
        "reason": "memory_runtime_unpublished",
        "release_state": "unavailable",
        "download_error": None,
    }
    assert by["node"]["installed"] and by["node"]["version"] == "20.11"


@pytest.mark.parametrize(
    ("runtime_status", "expected"),
    (
        pytest.param(
            {
                "provider": "manifest-cache",
                "install": {"state": "installed", "runtime_version": "runtime-installed", "matches_manifest": False},
                "manifest": {"runtime_version": "runtime-selected"},
                "node_available": True,
                "node_supported": True,
                "node_version": "22.12.0",
            },
            {"version": "runtime-installed", "latest_version": "runtime-selected", "has_update": True},
            id="stale-manifest-install",
        ),
        pytest.param(
            {
                "provider": "npm",
                "install": {"state": "installed", "runtime_version": None, "matches_manifest": None},
                "manifest": None,
                "node_available": True,
                "node_supported": True,
                "node_version": "22.12.0",
            },
            {"version": None, "latest_version": None, "has_update": False},
            id="npm-not-comparable",
        ),
    ),
)
def test_dependencies_status_projects_show_runtime_identity_without_pairing(monkeypatch, runtime_status, expected):
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {"installed": True, "version": "0.1.14", "latest_version": None, "has_update": False, "status": "ready"},
    )
    monkeypatch.setattr(
        api,
        "avault_status",
        lambda: {"installed": True, "version": "0.0.1", "status": "ready"},
    )
    monkeypatch.setattr(
        api.V2Config,
        "load",
        classmethod(lambda _cls: SimpleNamespace(memory=SimpleNamespace(enabled=False))),
    )

    import avibe_memory.artifact as memory_artifact
    import core.show_runtime as show_runtime
    import core.tmux_runtime as tmux_runtime

    manager = Mock()
    manager.status.return_value = runtime_status
    memory_manager = Mock()
    memory_manager.status.return_value = {"installed": False, "status": "missing", "manifest": None}
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)
    monkeypatch.setattr(memory_artifact, "get_memory_artifact_manager", lambda: memory_manager)
    monkeypatch.setattr(tmux_runtime, "tmux_status", lambda: {"installed": False, "version": None, "status": "missing"})

    entry = next(item for item in api.dependencies_status()["deps"] if item["id"] == "show-runtime")

    assert entry["installed"] is True
    assert entry["status"] == "ready"
    assert {key: entry[key] for key in expected} == expected


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        ({"installed": False, "reason": "memory_runtime_platform_unsupported"}, "unsupported"),
        ({"installed": False, "reason": "memory_runtime_install_failed"}, "error"),
        ({"installed": False, "status": "error", "reason": None}, "error"),
        ({"installed": False, "reason": "memory_runtime_unpublished"}, "missing"),
    ],
)
def test_memory_runtime_dependency_status_maps_closed_failures(runtime, expected) -> None:
    assert api._memory_runtime_dependency_status(runtime) == expected


def test_memory_runtime_dependency_job_uses_controller_lifecycle(monkeypatch):
    from vibe import internal_client

    calls: list[bool] = []

    def install_runtime() -> dict:
        calls.append(True)
        return {
            "status_code": 200,
            "body": {"ok": False, "reason": "memory_runtime_unpublished", "download_error": None},
        }

    monkeypatch.setattr(internal_client, "memory_install_runtime_sync", install_runtime)

    assert api._prepare_memory_runtime_job() == {
        "ok": False,
        "message": "memory_runtime_unpublished",
        "output": None,
        "reason": "memory_runtime_unpublished",
        "download_error": None,
    }
    assert calls == [True]


def test_memory_runtime_dependency_job_preserves_controller_closed_error(monkeypatch):
    from vibe import internal_client

    monkeypatch.setattr(
        internal_client,
        "memory_install_runtime_sync",
        lambda: {"status_code": 503, "body": {"ok": False, "reason": "memory_runtime_missing"}},
    )

    assert api._prepare_memory_runtime_job() == {
        "ok": False,
        "message": "memory_runtime_missing",
        "output": None,
        "reason": "memory_runtime_missing",
        "download_error": None,
    }


def test_memory_runtime_dependency_job_restarts_after_repairing_unavailable_factory(
    monkeypatch,
):
    from vibe import internal_client

    controller_calls: list[bool] = []
    installs: list[bool] = []
    restarts: list[dict] = []

    def install_runtime() -> dict:
        controller_calls.append(True)
        return {
            "status_code": 503,
            # This token also covers an imported implementation whose factory
            # is absent or raises, so this process cannot safely retry it.
            "body": {"ok": False, "reason": "memory_plugin_unavailable"},
        }

    monkeypatch.setattr(
        internal_client,
        "memory_install_runtime_sync",
        install_runtime,
    )
    monkeypatch.setattr(
        api,
        "_install_memory_package_for_current_release",
        lambda: installs.append(True) or {"ok": True, "output": "installed"},
    )
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/bin/vibe")
    monkeypatch.setattr(
        api,
        "schedule_restart",
        lambda **kwargs: restarts.append(kwargs) or {"ok": True, "job_id": "memory-repair"},
    )

    assert api._prepare_memory_runtime_job() == {
        "ok": True,
        "message": "memory_runtime_restart_scheduled",
        "output": "installed",
        "reason": None,
        "download_error": None,
        "restarting": True,
    }
    assert controller_calls == [True]
    assert installs == [True]
    assert restarts == [
        {
            "delay_seconds": 2.0,
            "vibe_path": "/bin/vibe",
            "trigger": "memory-package-repair",
        }
    ]


def test_memory_runtime_dependency_job_restarts_after_repairing_incompatible_package(
    monkeypatch,
):
    from vibe import internal_client

    controller_calls: list[bool] = []
    package_installs: list[bool] = []
    restarts: list[dict] = []

    def install_runtime() -> dict:
        controller_calls.append(True)
        return {
            "status_code": 503,
            "body": {"ok": False, "reason": "memory_plugin_incompatible"},
        }

    monkeypatch.setattr(internal_client, "memory_install_runtime_sync", install_runtime)
    monkeypatch.setattr(
        api,
        "_install_memory_package_for_current_release",
        lambda: package_installs.append(True) or {"ok": True, "output": "installed"},
    )
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/bin/vibe")
    monkeypatch.setattr(
        api,
        "schedule_restart",
        lambda **kwargs: restarts.append(kwargs) or {"ok": True, "job_id": "memory-repair"},
    )

    assert api._prepare_memory_runtime_job() == {
        "ok": True,
        "message": "memory_runtime_restart_scheduled",
        "output": "installed",
        "reason": None,
        "download_error": None,
        "restarting": True,
    }
    assert controller_calls == [True]
    assert package_installs == [True]
    assert restarts == [
        {
            "delay_seconds": 2.0,
            "vibe_path": "/bin/vibe",
            "trigger": "memory-package-repair",
        }
    ]


def test_memory_runtime_dependency_job_requires_restart_when_scheduling_fails(
    monkeypatch,
):
    from vibe import internal_client

    controller_calls: list[bool] = []

    def install_runtime() -> dict:
        controller_calls.append(True)
        return {
            "status_code": 503,
            "body": {"ok": False, "reason": "memory_plugin_incompatible"},
        }

    monkeypatch.setattr(internal_client, "memory_install_runtime_sync", install_runtime)
    monkeypatch.setattr(
        api,
        "_install_memory_package_for_current_release",
        lambda: {"ok": True, "output": "installed"},
    )

    def fail_schedule(**kwargs):
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(
        api,
        "schedule_restart",
        fail_schedule,
    )

    assert api._prepare_memory_runtime_job() == {
        "ok": False,
        "message": "memory_runtime_restart_required",
        "output": "scheduler unavailable",
        "reason": "memory_runtime_restart_required",
        "download_error": None,
        "restarting": False,
    }
    assert controller_calls == [True]


def test_memory_runtime_dependency_job_stops_when_package_resolution_fails(monkeypatch):
    from vibe import internal_client

    calls: list[bool] = []
    monkeypatch.setattr(
        internal_client,
        "memory_install_runtime_sync",
        lambda: {
            "status_code": 503,
            "body": {"ok": False, "reason": "memory_plugin_incompatible"},
        },
    )
    monkeypatch.setattr(
        api,
        "_install_memory_package_for_current_release",
        lambda: calls.append(True)
        or {
            "ok": False,
            "message": "memory_plugin_incompatible",
            "output": "resolver failed",
            "reason": "memory_plugin_incompatible",
            "download_error": None,
        },
    )

    assert api._prepare_memory_runtime_job() == {
        "ok": False,
        "message": "memory_plugin_incompatible",
        "output": "resolver failed",
        "reason": "memory_plugin_incompatible",
        "download_error": None,
    }
    assert calls == [True]


def test_memory_package_install_uses_the_non_replacing_add_plan(monkeypatch):
    from vibe import __version__

    plan = object()
    planned: list[dict] = []
    executed: list[object] = []
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/bin/vibe")
    monkeypatch.setattr(
        api,
        "installed_metadata_describes_running_code",
        lambda: True,
    )
    monkeypatch.setattr(api, "installed_package_name", lambda: "avibe-os")
    monkeypatch.setattr(api, "package_mutation_lock", nullcontext)
    monkeypatch.setattr(
        api,
        "build_memory_add_plan",
        lambda **kwargs: planned.append(kwargs) or plan,
    )
    monkeypatch.setattr(
        api,
        "execute_upgrade_plan",
        lambda selected, **kwargs: executed.append(selected)
        or subprocess.CompletedProcess([], 0, stdout="installed", stderr=""),
    )

    result = api._install_memory_package_for_current_release()

    assert result["ok"] is True
    assert planned == [
        {
            "vibe_path": "/bin/vibe",
            "version": __version__,
            "package_name": "avibe-os",
        }
    ]
    assert executed == [plan]


def test_memory_package_install_refuses_a_stale_pre_restart_process(monkeypatch):
    monkeypatch.setattr(api, "package_mutation_lock", nullcontext)
    monkeypatch.setattr(
        api,
        "installed_metadata_describes_running_code",
        lambda: False,
    )
    monkeypatch.setattr(
        api,
        "build_memory_add_plan",
        lambda **kwargs: pytest.fail("stale code must not pin or mutate the replaced install"),
    )

    result = api._install_memory_package_for_current_release()

    assert result == {
        "ok": False,
        "message": "memory_runtime_install_failed",
        "output": "Avibe changed on disk; restart before installing Memory.",
        "reason": "memory_runtime_install_failed",
        "download_error": None,
    }


def test_dependencies_status_node_unsupported_not_ready(monkeypatch):
    # Node present but below the runtime minimum (node_supported False) -> not ready.
    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {"id": "askill", "installed": True, "version": "0.1.13", "status": "ready", "path": "/x"},
    )
    monkeypatch.setattr(
        api,
        "avault_status",
        lambda: {"id": "avault", "installed": True, "version": "0.0.1", "status": "ready", "path": "/x/avault"},
    )
    import core.show_runtime as srt_mod

    class _Mgr:
        def status(self):
            return {"installed": False, "manifest": None, "node_available": True, "node_supported": False, "node_version": "16.0"}

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())
    by = {d["id"]: d for d in api.dependencies_status()["deps"]}
    assert by["node"]["installed"] is False and by["node"]["status"] == "missing"


def test_reconcile_startup_dependencies_uses_automatic_runtime_admission(monkeypatch):
    askill_calls = []
    avault_calls = []

    def fake_ensure(force=False):
        askill_calls.append(force)
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", fake_ensure)

    def fake_ensure_avault(force=False):
        avault_calls.append(force)
        return {"ok": True, "installed": True, "changed": False, "path": "/x/avault"}

    monkeypatch.setattr(api, "ensure_avault_installed", fake_ensure_avault)

    import core.show_runtime as srt_mod

    class _Mgr:
        def __init__(self):
            self.prepared = []

        def status(self, *, offline=False):
            assert offline is True
            return {
                "node_available": True,
                "node_supported": True,
                "node_version": "22.12.0",
            }

        def prepare(self, *, force=False, automatic=False):
            self.prepared.append((force, automatic))
            return {
                "policy": {"state": "allowed", "reason": None},
                "install": {"state": "installed", "reason": None},
                "runtime": {"state": "unchecked", "reason": None},
                "status": {
                    "node_available": True,
                    "node_supported": True,
                    "node_version": "22.12.0",
                },
            }

    manager = _Mgr()
    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: manager)

    out = api.reconcile_startup_dependencies()

    assert out["ok"] is True
    assert askill_calls == [False]
    assert avault_calls == [False]
    assert manager.prepared == [(False, True)]
    assert out["node"]["status"] == "ready"
    assert out["show_runtime"]["ok"] is True
    assert out["show_runtime"]["status"] == "pending_prewarm"
    assert out["show_runtime"]["policy"]["state"] == "allowed"
    assert out["show_runtime"]["install"]["state"] == "installed"
    assert out["show_runtime"]["runtime"]["state"] == "unchecked"


def test_reconcile_startup_dependencies_reports_runtime_install_failure_without_node(monkeypatch):
    monkeypatch.setattr(api, "ensure_askill_installed", lambda force=False: {"ok": True, "installed": True})
    monkeypatch.setattr(api, "ensure_avault_installed", lambda force=False: {"ok": True, "installed": True})

    import core.show_runtime as srt_mod

    class _Mgr:
        def __init__(self):
            self.prepared = []

        def status(self, *, offline=False):
            assert offline is True
            return {
                "node_available": False,
                "node_supported": None,
                "node_version": None,
            }

        def prepare(self, *, force=False, automatic=False):
            self.prepared.append((force, automatic))
            pytest.fail("a missing prerequisite must not enter install admission")

    manager = _Mgr()
    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: manager)

    out = api.reconcile_startup_dependencies()

    assert out["ok"] is False
    assert manager.prepared == []
    assert out["node"]["status"] == "missing"
    assert out["show_runtime"]["ok"] is False
    assert out["show_runtime"]["status"] == "failed"
    assert out["show_runtime"]["install"]["state"] == "failed"
    assert out["show_runtime"]["install"]["reason"] == "runtime_node_missing"


def test_reconcile_startup_dependencies_can_be_disabled(monkeypatch):
    monkeypatch.setenv("VIBE_STARTUP_DEPENDENCY_RECONCILE", "0")
    monkeypatch.setattr(api, "ensure_askill_installed", lambda force=False: pytest.fail("should not reconcile"))
    monkeypatch.setattr(api, "ensure_avault_installed", lambda force=False: pytest.fail("should not reconcile"))

    out = api.reconcile_startup_dependencies()

    assert out == {"ok": True, "skipped": True, "reason": "disabled"}


def test_startup_show_page_prewarm_targets_recent_non_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from config import paths
    from core.show_pages import ShowPageStore

    paths.ensure_data_dirs()
    store = ShowPageStore()
    try:
        store.ensure("ses-old")
        store.update_visibility("ses-public", "public")
        limited = store.ensure("ses-limited")
        result = store.apply_access(
            "ses-limited",
            expected_revision=limited.access_revision,
            target_access_mode="limited",
            target_share_id=limited.share_id,
            target_emails=["viewer@example.com"],
        )
        assert result.status == "applied"
        store.update_visibility("ses-offline", "offline")
        store.ensure("ses-new")
    finally:
        store.close()

    out = api.startup_show_page_prewarm_targets(limit=3)

    assert out["limit"] == 3
    assert [page["session_id"] for page in out["pages"]] == [
        "ses-new",
        "ses-limited",
        "ses-public",
    ]
    assert out["pages"][0]["context"] == "private"
    assert out["pages"][1]["visibility"] == "limited"
    assert out["pages"][1]["context"] == "private"
    assert out["pages"][2]["visibility"] == "public"
    assert out["pages"][2]["context"] == "shared"
    assert all("base_path" not in page for page in out["pages"])


def test_startup_show_page_prewarm_limit_env(monkeypatch):
    monkeypatch.setenv("VIBE_STARTUP_SHOW_PAGE_PREWARM_LIMIT", "0")
    assert api.startup_show_page_prewarm_limit() == 0

    monkeypatch.setenv("VIBE_STARTUP_SHOW_PAGE_PREWARM_LIMIT", "99")
    assert api.startup_show_page_prewarm_limit() == 10


def test_start_dependency_install_job_rejects_unknown():
    assert api.start_dependency_install_job("bogus")["ok"] is False


def test_prepare_show_runtime_job_surfaces_retry_diagnostics(monkeypatch):
    import core.show_runtime as show_runtime

    manager = Mock()
    manager.prepare.return_value = {
        "ok": False,
        "reason": "runtime_archive_download_failed",
        "install": {
            "state": "failed",
            "reason": "runtime_archive_download_failed",
            "failure_class": "transient",
        },
        "status": {
            "download_error": {
                "kind": "timeout",
                "message": "Connection timed out",
                "url": "https://example.test/runtime.tgz",
                "retryable": True,
                "attempts": 3,
            }
        },
    }
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)

    result = api._prepare_show_runtime_job()

    assert result["ok"] is False
    assert result["reason"] == "runtime_archive_download_failed"
    assert result["download_error"]["attempts"] == 3
    assert "after 3 attempts" in result["message"]


def test_prepare_show_runtime_job_reports_failed_replacement_with_old_install(monkeypatch):
    import core.show_runtime as show_runtime

    manager = Mock()
    manager.prepare.return_value = {
        "ok": False,
        "reason": "runtime_archive_download_failed",
        "install": {
            "state": "installed",
            "reason": None,
        },
        "status": {
            "installed": True,
            "command": ["node", "runtime-cli.js"],
        },
    }
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)

    result = api._prepare_show_runtime_job()

    assert result["ok"] is False
    assert result["reason"] == "runtime_archive_download_failed"
    assert "runtime_archive_download_failed" in result["message"]


def test_start_dependency_install_job_runs_askill(monkeypatch):
    import time as _t

    flag = {"called": False}

    def fake_ensure(force=False):
        flag["called"] = True
        return {"ok": True, "installed": True, "changed": True, "path": "/x/askill"}

    monkeypatch.setattr(api, "ensure_askill_installed", fake_ensure)
    job = api.start_dependency_install_job("askill")
    # Don't assert status=="running": an instant (mocked) worker can finish
    # before the snapshot is taken. Real installs are slow, so the UI still
    # observes "running" + polls. Verify completion via the poller below.
    assert job["ok"] and job["backend"] == "askill" and job.get("job_id")
    cur = job
    for _ in range(100):
        cur = api.get_agent_install_job(job["job_id"], backend="askill")
        if cur.get("status") != "running":
            break
        _t.sleep(0.02)
    assert flag["called"] is True
    assert cur["status"] == "succeeded" and cur["ok"] is True


def test_start_dependency_install_job_runs_avault(monkeypatch):
    import time as _t

    flag = {"called": False}

    def fake_ensure(force=False):
        flag["called"] = True
        return {"ok": True, "installed": True, "changed": False, "path": "/x/avault"}

    monkeypatch.setattr(api, "ensure_avault_installed", fake_ensure)
    job = api.start_dependency_install_job("avault")
    assert job["ok"] and job["backend"] == "avault" and job.get("job_id")
    cur = job
    for _ in range(100):
        cur = api.get_agent_install_job(job["job_id"], backend="avault")
        if cur.get("status") != "running":
            break
        _t.sleep(0.02)
    assert flag["called"] is True
    assert cur["status"] == "succeeded" and cur["ok"] is True
