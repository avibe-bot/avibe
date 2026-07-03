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
import tarfile
from unittest.mock import Mock

import pytest

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
    assert "curl -fsSL https://askill.sh | sh" in captured["cmd"][2]


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
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: api.AVAULT_GRANT_DELIVERY_MIN_VERSION)
    flag = {"installed": False}
    monkeypatch.setattr(api, "install_avault", lambda force=False: flag.__setitem__("installed", True) or {"ok": True})

    out = api.ensure_avault_installed()

    assert out == {
        "ok": True,
        "installed": True,
        "changed": False,
        "path": "/usr/local/bin/avault",
        "version": api.AVAULT_GRANT_DELIVERY_MIN_VERSION,
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
    monkeypatch.setattr(api, "_probe_avault_version", lambda _path: api.AVAULT_GRANT_DELIVERY_MIN_VERSION)
    monkeypatch.setattr(api, "install_avault", lambda force=False: pytest.fail("should not downgrade avault"))

    out = api.ensure_avault_installed(force=True)

    assert out == {
        "ok": True,
        "installed": True,
        "changed": False,
        "path": "/usr/local/bin/avault",
        "version": api.AVAULT_GRANT_DELIVERY_MIN_VERSION,
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
        stdout = f"avault {api.AVAULT_GRANT_DELIVERY_MIN_VERSION}\n"
        stderr = ""

    monkeypatch.setattr(api.subprocess, "run", lambda *a, **k: _R())
    s = api.avault_status()
    assert s["installed"] and s["version"] == api.AVAULT_GRANT_DELIVERY_MIN_VERSION and s["status"] == "ready"


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


def test_dependencies_status_shape(monkeypatch):
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
            return {"installed": True, "manifest": {"runtime_version": "1.4.0"}, "node_available": True, "node_version": "20.11"}

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())
    out = api.dependencies_status()
    assert out["ok"]
    by = {d["id"]: d for d in out["deps"]}
    assert list(by) == ["askill", "avault", "show-runtime", "tmux", "node"]
    assert "tmux" in by and by["tmux"]["required"] is False  # tmux is the optional terminal backend
    assert by["askill"]["status"] == "ready" and by["askill"]["version"] == "0.1.13" and by["askill"]["required"]
    assert by["askill"]["latest_version"] is None and by["askill"]["has_update"] is False
    assert by["avault"]["status"] == "ready" and by["avault"]["version"] == "0.0.1" and by["avault"]["required"]
    assert by["avault"]["latest_version"] is None and by["avault"]["has_update"] is False
    assert by["show-runtime"]["installed"] and by["show-runtime"]["version"] == "1.4.0"
    assert by["node"]["installed"] and by["node"]["version"] == "20.11"


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


def test_reconcile_startup_dependencies_installs_askill_and_defers_runtime_prepare(monkeypatch):
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

        def status(self):
            return {
                "installed": False,
                "manifest": {"runtime_version": "1.4.0"},
                "node_available": True,
                "node_supported": True,
                "node_version": "22.12.0",
            }

        def prepare(self, *, force=False):
            self.prepared.append(force)
            return {"ok": True, "reason": None}

    manager = _Mgr()
    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: manager)

    out = api.reconcile_startup_dependencies()

    assert out["ok"] is True
    assert askill_calls == [False]
    assert avault_calls == [False]
    assert manager.prepared == []
    assert out["node"]["status"] == "ready"
    assert out["show_runtime"] == {"ok": True, "status": "pending_prewarm", "reason": None}


def test_reconcile_startup_dependencies_does_not_prepare_runtime_without_node(monkeypatch):
    monkeypatch.setattr(api, "ensure_askill_installed", lambda force=False: {"ok": True, "installed": True})
    monkeypatch.setattr(api, "ensure_avault_installed", lambda force=False: {"ok": True, "installed": True})

    import core.show_runtime as srt_mod

    class _Mgr:
        def status(self):
            return {"installed": False, "node_available": False, "node_version": None}

        def prepare(self, *, force=False):
            raise AssertionError("runtime must not prepare without Node")

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())

    out = api.reconcile_startup_dependencies()

    assert out["ok"] is False
    assert out["node"]["status"] == "missing"
    assert out["show_runtime"] == {"ok": False, "status": "skipped", "reason": "runtime_node_missing"}


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
        store.update_visibility("ses-offline", "offline")
        store.ensure("ses-new")
    finally:
        store.close()

    out = api.startup_show_page_prewarm_targets(limit=2)

    assert out["limit"] == 2
    assert [page["session_id"] for page in out["pages"]] == ["ses-new", "ses-public"]
    assert out["pages"][1]["visibility"] == "public"
    assert out["pages"][1]["base_path"].startswith("/p/")


def test_startup_show_page_prewarm_limit_env(monkeypatch):
    monkeypatch.setenv("VIBE_STARTUP_SHOW_PAGE_PREWARM_LIMIT", "0")
    assert api.startup_show_page_prewarm_limit() == 0

    monkeypatch.setenv("VIBE_STARTUP_SHOW_PAGE_PREWARM_LIMIT", "99")
    assert api.startup_show_page_prewarm_limit() == 10


def test_start_dependency_install_job_rejects_unknown():
    assert api.start_dependency_install_job("bogus")["ok"] is False


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
