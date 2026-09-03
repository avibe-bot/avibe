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
import sys
import tarfile
import urllib.error
from contextlib import nullcontext
from pathlib import Path
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
    def fake_candidate_cli_paths(binary: str, *, include_npm_global: bool = True):
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


def test_shared_install_runner_failures_have_structured_identity(monkeypatch):
    class FailedPopen:
        returncode = 17

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, timeout=None):
            return "", "installer stderr"

    monkeypatch.setattr(api.subprocess, "Popen", FailedPopen)
    failed = api._run_install_command("askill", ["bash"], lambda value: value)
    assert failed["reason"] == "askill_install_failed"
    assert failed["exit_code"] == 17

    class ErrorPopen:
        def __init__(self, *args, **kwargs):
            raise OSError("runner unavailable")

    monkeypatch.setattr(api.subprocess, "Popen", ErrorPopen)
    errored = api._run_install_command("askill", ["bash"], lambda value: value)
    assert errored["reason"] == "askill_install_error"
    assert errored["error"] == "runner unavailable"

    class TimedOutPopen:
        returncode = 0

        def __init__(self, *args, **kwargs):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise api.subprocess.TimeoutExpired(["bash"], timeout)
            return "partial", ""

    monkeypatch.setattr(api.subprocess, "Popen", TimedOutPopen)
    monkeypatch.setattr(api, "signal_process_tree", lambda *args, **kwargs: None)
    timed_out = api._run_install_command("askill", ["bash"], lambda value: value)
    assert timed_out["reason"] == "askill_install_timeout"
    assert timed_out["timeout_seconds"] == 300


def test_install_askill_unsupported_without_curl(monkeypatch):
    # No curl/bash (e.g. Windows): no broken npm fallback — a clear manual
    # message pointing at askill.sh, and _run_install_command is never invoked.
    monkeypatch.setattr(api, "resolve_cli_path", lambda b: None)
    monkeypatch.setattr(api, "_run_install_command", lambda *a, **k: pytest.fail("should not install"))
    out = api.install_askill()
    assert out["ok"] is False
    assert "askill.sh" in out["message"]
    assert out["reason"] == "askill_auto_install_unsupported"
    assert out["required_tools"] == ["curl", "bash"]


def test_install_askill_unsupported_on_windows_even_with_tools(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr(api, "resolve_cli_path", lambda binary: f"C:/{binary}.exe")
    monkeypatch.setattr(api, "_run_install_command", lambda *a, **k: pytest.fail("should not install"))

    out = api.install_askill()

    assert out["ok"] is False
    assert "askill.sh" in out["message"]
    assert out["reason"] == "askill_auto_install_unsupported"


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
    assert out["reason"] == "askill_install_path_missing"
    assert out["expected_path"] == "askill"


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
    assert out["reason"] == "avault_platform_unsupported"
    assert out["platform"] == "FreeBSD-riscv64"


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
    assert out["reason"] == "avault_checksum_mismatch"
    assert out["expected_sha256"] == "0" * 64
    assert len(out["actual_sha256"]) == 64
    assert not installed.exists()


def test_install_avault_generic_failure_has_structured_identity(monkeypatch):
    monkeypatch.setattr(api, "_configured_avault_cli_path", lambda: "avault")
    monkeypatch.setattr(api, "resolve_cli_path", lambda _binary: None)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr(
        api,
        "_download_avault_release_file",
        lambda _url: (_ for _ in ()).throw(ValueError("invalid manifest")),
    )

    out = api.install_avault()

    assert out["ok"] is False
    assert out["reason"] == "avault_install_failed"
    assert out["error"] == "invalid manifest"


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
        classmethod(lambda _cls: SimpleNamespace(memory_required=True)),
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
    monkeypatch.setattr(
        api,
        "_memory_dependencies_status",
        lambda **_: (
            {
                "id": "memory-package",
                "kind": "runtime",
                "required": True,
                "installed": True,
                "provider_count": 1,
                "version": "1.2.3",
                "latest_version": "1.2.3",
                "has_update": False,
                "status": "ready",
                "readiness": "ready",
                "reason": None,
                "action_class": "none",
                "warnings": [],
            },
            {
                "id": "memory-runtime",
                "kind": "runtime",
                "required": True,
                "installed": False,
                "version": None,
                "latest_version": "1.2.3",
                "has_update": False,
                "status": "missing",
                "reason": "memory_runtime_unpublished",
                "action_class": "repairable",
                "release_state": "unavailable",
                "download_error": None,
            },
        ),
    )
    out = api.dependencies_status()
    assert out["ok"]
    by = {d["id"]: d for d in out["deps"]}
    assert list(by) == [
        "askill",
        "avault",
        "show-runtime",
        "memory-package",
        "memory-runtime",
        "tmux",
        "node",
    ]
    assert "tmux" in by and by["tmux"]["required"] is False  # tmux is the optional terminal backend
    assert by["askill"]["status"] == "ready" and by["askill"]["version"] == "0.1.13" and by["askill"]["required"]
    assert by["askill"]["latest_version"] is None and by["askill"]["has_update"] is False
    assert by["avault"]["status"] == "ready" and by["avault"]["version"] == "0.0.1" and by["avault"]["required"]
    assert by["avault"]["latest_version"] is None and by["avault"]["has_update"] is False
    assert by["show-runtime"]["installed"] and by["show-runtime"]["version"] == "1.4.0"
    assert by["show-runtime"]["latest_version"] == "1.4.0"
    assert by["show-runtime"]["has_update"] is False
    assert by["memory-package"]["readiness"] == "ready"
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
        "action_class": "repairable",
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

    import core.show_runtime as show_runtime
    import core.tmux_runtime as tmux_runtime

    manager = Mock()
    manager.status.return_value = runtime_status
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)
    monkeypatch.setattr(
        api,
        "_memory_dependencies_status",
        lambda **_: (
            {"id": "memory-package", "status": "not_required"},
            {"id": "memory-runtime", "status": "not_required"},
        ),
    )
    monkeypatch.setattr(tmux_runtime, "tmux_status", lambda: {"installed": False, "version": None, "status": "missing"})

    entry = next(item for item in api.dependencies_status()["deps"] if item["id"] == "show-runtime")

    assert entry["installed"] is True
    assert entry["status"] == "ready"
    assert {key: entry[key] for key in expected} == expected


def _stub_dependency_status_neighbors(monkeypatch) -> None:
    import core.tmux_runtime as tmux_runtime

    monkeypatch.setattr(
        api,
        "askill_update_status",
        lambda **_: {
            "installed": True,
            "version": "0.1.14",
            "latest_version": None,
            "has_update": False,
            "status": "ready",
        },
    )
    monkeypatch.setattr(
        api,
        "avault_status",
        lambda: {"installed": True, "version": "0.0.1", "status": "ready"},
    )
    monkeypatch.setattr(
        api,
        "_memory_dependencies_status",
        lambda **_: (
            {"id": "memory-package", "status": "not_required"},
            {"id": "memory-runtime", "status": "not_required"},
        ),
    )
    monkeypatch.setattr(
        tmux_runtime,
        "tmux_status",
        lambda: {"installed": False, "version": None, "status": "missing"},
    )


def test_dependencies_status_preserves_show_runtime_inspection_failure(monkeypatch, tmp_path):
    import core.show_runtime as show_runtime

    _stub_dependency_status_neighbors(monkeypatch)
    runtime_dir = tmp_path / "runtime"
    pointer = runtime_dir / "prebuilt" / "current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps(
            {
                "provider": "archive",
                "runtime_id": "show-runtime",
                "install_dir": str(runtime_dir / "prebuilt" / "versions" / "missing"),
            }
        ),
        encoding="utf-8",
    )
    manager = show_runtime.ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=runtime_dir,
        runtime_source="archive",
    )
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)
    pointer_before = pointer.read_bytes()

    entry = next(
        item
        for item in api.dependencies_status()["deps"]
        if item["id"] == "show-runtime"
    )

    assert entry["installed"] is None
    assert entry["status"] == "error"
    assert entry["action_class"] == "operator_only"
    assert entry["reason"] == "runtime_install_inspection_failed"
    assert entry["inspection_error"]["kind"] == "OSError"
    repair = manager.repair()
    assert repair["reason"] == "runtime_install_inspection_failed"
    assert repair["repair_attempted"] is False
    assert pointer.read_bytes() == pointer_before


def test_dependencies_status_keeps_true_show_runtime_absence_installable(monkeypatch, tmp_path):
    import core.show_runtime as show_runtime

    _stub_dependency_status_neighbors(monkeypatch)
    manager = show_runtime.ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="archive",
        archive_path=tmp_path / "runtime.tgz",
    )
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)

    entry = next(
        item
        for item in api.dependencies_status()["deps"]
        if item["id"] == "show-runtime"
    )

    assert entry["installed"] is False
    assert entry["status"] == "missing"
    assert entry["action_class"] == "repairable"
    assert entry["reason"] is None


def test_show_runtime_status_does_not_hide_programming_defects(monkeypatch, tmp_path):
    from core.show_runtime import ShowRuntimeManager

    manager = ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(manager, "_status", lambda **_kwargs: (_ for _ in ()).throw(TypeError("bug")))

    with pytest.raises(TypeError, match="bug"):
        manager.status()


def test_settings_and_doctor_consume_the_same_verified_repair_owner(monkeypatch, tmp_path):
    import core.show_runtime as show_runtime
    from vibe import cli

    manager = show_runtime.ShowRuntimeManager(
        command="/bin/echo",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        auto_install=False,
    )
    verification_calls = []

    def verify(command):
        verification_calls.append(command)
        return show_runtime.ShowRuntimeStartability.startable()

    monkeypatch.setattr(manager, "_verify_startability", verify)
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)
    monkeypatch.setattr(show_runtime, "ShowRuntimeManager", lambda: manager)

    settings = api._prepare_show_runtime_job()
    doctor = cli._repair_show_runtime()

    assert settings["outcome"] == "healthy"
    assert settings["changed"] is False
    assert doctor["status"] == "skipped"
    assert verification_calls == [["/bin/echo"], ["/bin/echo"]]


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


@pytest.mark.parametrize(
    ("metadata", "status", "reason", "action_class"),
    (
        (api._MemoryPackageMetadata(0, None), "missing", "memory_package_missing", "repairable"),
        (
            api._MemoryPackageMetadata(2, None),
            "error",
            "memory_package_metadata_ambiguous",
            "operator_only",
        ),
        (
            api._MemoryPackageMetadata(None, None),
            "error",
            "memory_package_metadata_unreadable",
            "operator_only",
        ),
        (
            api._MemoryPackageMetadata(1, None),
            "error",
            "memory_package_metadata_unreadable",
            "operator_only",
        ),
        (
            api._MemoryPackageMetadata(1, "3.0.15"),
            "error",
            "memory_package_version_mismatch",
            "repairable",
        ),
    ),
)
def test_required_memory_package_metadata_precedes_optional_imports(
    monkeypatch,
    metadata,
    status,
    reason,
    action_class,
) -> None:
    monkeypatch.setattr(
        api,
        "_load_memory_requirement",
        lambda: api._MemoryRequirementProjection(True, "required"),
    )
    monkeypatch.setattr(api, "_inspect_memory_package_metadata", lambda: metadata)
    monkeypatch.setattr(api, "_published_running_version", lambda: "3.0.14")
    monkeypatch.setattr(api, "get_build_identity", lambda: SimpleNamespace(kind="package"))
    monkeypatch.setattr(api, "_memory_package_restart_retry_required", lambda _version: False)
    probe = Mock(side_effect=AssertionError("metadata failure imported Memory runtime"))
    artifact = Mock(side_effect=AssertionError("metadata failure imported Memory artifact"))
    monkeypatch.setattr(api, "probe_memory_runtime_entrypoint", probe)
    monkeypatch.setattr(api, "_memory_artifact_status", artifact)

    package, runtime = api._memory_dependencies_status(offline=True)

    assert package["status"] == status
    assert package["reason"] == reason
    assert package["action_class"] == action_class
    assert runtime["reason"] == reason
    probe.assert_not_called()
    artifact.assert_not_called()


@pytest.mark.parametrize(
    (
        "requirement",
        "metadata",
        "build_kind",
        "current_version",
        "probe_failure",
        "artifact_imported",
        "expected",
    ),
    (
        pytest.param(
            api._MemoryRequirementProjection(False, "not_required"),
            api._MemoryPackageMetadata(1, "3.0.13"),
            "package",
            "3.0.14",
            False,
            True,
            ("error", "not_required", "memory_package_version_mismatch", "repairable"),
            id="not-required-exposes-version-bootstrap",
        ),
        pytest.param(
            api._MemoryRequirementProjection(False, "not_required"),
            api._MemoryPackageMetadata(0, None),
            "package",
            "3.0.14",
            False,
            True,
            ("missing", "not_required", "memory_package_missing", "repairable"),
            id="not-required-exposes-missing-bootstrap",
        ),
        pytest.param(
            api._MemoryRequirementProjection(False, "not_required"),
            api._MemoryPackageMetadata(1, "3.0.14"),
            "package",
            "3.0.14",
            True,
            True,
            ("not_required", "not_required", None, "repairable"),
            id="not-required-exact-package-keeps-explicit-repair",
        ),
        pytest.param(
            api._MemoryRequirementProjection(False, "not_required"),
            api._MemoryPackageMetadata(None, None),
            "package",
            "3.0.14",
            False,
            True,
            ("error", "not_required", "memory_package_metadata_unreadable", "operator_only"),
            id="not-required-unreadable-metadata-stays-operator-only",
        ),
        pytest.param(
            api._MemoryRequirementProjection(False, "not_required"),
            api._MemoryPackageMetadata(2, None),
            "package",
            "3.0.14",
            False,
            True,
            ("error", "not_required", "memory_package_metadata_ambiguous", "operator_only"),
            id="not-required-ambiguous-metadata-stays-operator-only",
        ),
        pytest.param(
            api._MemoryRequirementProjection(False, "not_required"),
            api._MemoryPackageMetadata(0, None),
            "source",
            "3.0.14",
            False,
            True,
            ("not_required", "not_required", None, "none"),
            id="not-required-source-stays-operator-owned",
        ),
        pytest.param(
            api._MemoryRequirementProjection(True, "required"),
            api._MemoryPackageMetadata(1, "3.0.14"),
            "package",
            "3.0.14",
            False,
            True,
            ("ready", "ready", None, "none"),
            id="required-ready",
        ),
        pytest.param(
            api._MemoryRequirementProjection(True, "required"),
            api._MemoryPackageMetadata(0, None),
            "package",
            "3.0.14",
            False,
            True,
            ("missing", "not_ready", "memory_package_missing", "repairable"),
            id="required-missing",
        ),
        pytest.param(
            api._MemoryRequirementProjection(True, "required"),
            api._MemoryPackageMetadata(1, "3.0.13"),
            "package",
            "3.0.14",
            False,
            True,
            ("error", "not_ready", "memory_package_version_mismatch", "repairable"),
            id="required-version-mismatch",
        ),
        pytest.param(
            api._MemoryRequirementProjection(True, "required"),
            api._MemoryPackageMetadata(1, "3.0.14"),
            "package",
            "3.0.14",
            True,
            True,
            ("error", "not_ready", "memory_package_runtime_unavailable", "repairable"),
            id="required-runtime-probe-broken",
        ),
        pytest.param(
            api._MemoryRequirementProjection(True, "required"),
            api._MemoryPackageMetadata(1, "3.0.14"),
            "package",
            "3.0.14",
            False,
            False,
            ("error", "not_ready", "memory_package_artifact_unavailable", "repairable"),
            id="required-artifact-import-broken",
        ),
        pytest.param(
            api._MemoryRequirementProjection(True, "required"),
            api._MemoryPackageMetadata(None, None),
            "package",
            "3.0.14",
            False,
            True,
            ("error", "not_ready", "memory_package_metadata_unreadable", "operator_only"),
            id="operator-only-unreadable-metadata",
        ),
        pytest.param(
            api._MemoryRequirementProjection(True, "required"),
            api._MemoryPackageMetadata(2, None),
            "package",
            "3.0.14",
            False,
            True,
            ("error", "not_ready", "memory_package_metadata_ambiguous", "operator_only"),
            id="operator-only-duplicate-provider",
        ),
        pytest.param(
            api._MemoryRequirementProjection(True, "required"),
            api._MemoryPackageMetadata(1, "3.0.14"),
            "source",
            "3.0.14",
            False,
            True,
            ("error", "not_ready", "memory_package_source_build", "operator_only"),
            id="operator-only-source-deployment",
        ),
    ),
)
def test_memory_package_state_action_matrix(
    monkeypatch,
    requirement,
    metadata,
    build_kind,
    current_version,
    probe_failure,
    artifact_imported,
    expected,
) -> None:
    monkeypatch.setattr(api, "_load_memory_requirement", lambda: requirement)
    monkeypatch.setattr(api, "_inspect_memory_package_metadata", lambda: metadata)
    monkeypatch.setattr(api, "_published_running_version", lambda: current_version)
    monkeypatch.setattr(api, "get_build_identity", lambda: SimpleNamespace(kind=build_kind))
    monkeypatch.setattr(api, "_memory_package_restart_retry_required", lambda _version: False)
    monkeypatch.setattr(
        api,
        "probe_memory_runtime_entrypoint",
        Mock(side_effect=ImportError("broken entrypoint") if probe_failure else None),
    )
    monkeypatch.setattr(
        api,
        "_memory_artifact_status",
        lambda **_: (
            artifact_imported,
            {"installed": True, "status": "ready", "matches_manifest": True},
        ),
    )

    package, _runtime = api._memory_dependencies_status(offline=True)

    assert (
        package["status"],
        package["readiness"],
        package["reason"],
        package["action_class"],
    ) == expected


def test_required_memory_package_probe_and_artifact_order_keeps_package_ready(
    monkeypatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        api,
        "_load_memory_requirement",
        lambda: api._MemoryRequirementProjection(True, "required"),
    )
    monkeypatch.setattr(
        api,
        "_inspect_memory_package_metadata",
        lambda: api._MemoryPackageMetadata(1, "3.0.14"),
    )
    monkeypatch.setattr(api, "_published_running_version", lambda: "3.0.14")
    monkeypatch.setattr(api, "get_build_identity", lambda: SimpleNamespace(kind="package"))
    monkeypatch.setattr(
        api,
        "probe_memory_runtime_entrypoint",
        lambda: events.append("runtime-probe"),
    )

    def artifact_status(*, offline: bool) -> tuple[bool, dict]:
        assert offline is True
        events.append("artifact-import-and-status")
        return True, {
            "installed": False,
            "status": "error",
            "reason": "memory_runtime_install_failed",
        }

    monkeypatch.setattr(api, "_memory_artifact_status", artifact_status)

    package, runtime = api._memory_dependencies_status(offline=True)

    assert events == ["runtime-probe", "artifact-import-and-status"]
    assert package["readiness"] == "ready"
    assert package["reason"] is None
    assert runtime["status"] == "error"
    assert runtime["reason"] == "memory_runtime_install_failed"


def test_memory_package_restart_failure_remains_explicitly_repairable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api,
        "_load_memory_requirement",
        lambda: api._MemoryRequirementProjection(True, "required"),
    )
    monkeypatch.setattr(
        api,
        "_inspect_memory_package_metadata",
        lambda: api._MemoryPackageMetadata(1, "3.0.14"),
    )
    monkeypatch.setattr(api, "_published_running_version", lambda: "3.0.14")
    monkeypatch.setattr(api, "get_build_identity", lambda: SimpleNamespace(kind="package"))
    monkeypatch.setattr(api, "_memory_package_restart_retry_required", lambda _version: True)
    probe = Mock(side_effect=AssertionError("restart retry must not re-import the companion"))
    monkeypatch.setattr(api, "probe_memory_runtime_entrypoint", probe)

    package, runtime = api._memory_dependencies_status(offline=True)

    assert (package["status"], package["reason"], package["action_class"]) == (
        "error",
        "memory_package_restart_failed",
        "repairable",
    )
    assert runtime["reason"] == "memory_package_restart_failed"
    probe.assert_not_called()


@pytest.mark.parametrize(
    ("build_kind", "current_version"),
    (("source", "3.0.14"), ("package", "3.0.14")),
)
def test_mismatched_memory_runtime_keeps_repair_action_across_build_paths(
    monkeypatch,
    build_kind,
    current_version,
) -> None:
    monkeypatch.setattr(
        api,
        "_load_memory_requirement",
        lambda: api._MemoryRequirementProjection(True, "required"),
    )
    monkeypatch.setattr(
        api,
        "_inspect_memory_package_metadata",
        lambda: api._MemoryPackageMetadata(1, "3.0.14"),
    )
    monkeypatch.setattr(api, "_published_running_version", lambda: current_version)
    monkeypatch.setattr(api, "get_build_identity", lambda: SimpleNamespace(kind=build_kind))
    monkeypatch.setattr(api, "_memory_package_restart_retry_required", lambda _version: False)
    monkeypatch.setattr(api, "probe_memory_runtime_entrypoint", lambda: None)
    monkeypatch.setattr(
        api,
        "_memory_artifact_status",
        lambda **_: (
            True,
            {
                "installed": True,
                "status": "ready",
                "matches_manifest": False,
            },
        ),
    )

    _package, runtime = api._memory_dependencies_status(offline=True)

    assert runtime["has_update"] is True
    assert runtime["action_class"] == "repairable"


def test_required_memory_package_accepts_pep440_equivalent_versions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api,
        "_load_memory_requirement",
        lambda: api._MemoryRequirementProjection(True, "required"),
    )
    monkeypatch.setattr(
        api,
        "_inspect_memory_package_metadata",
        lambda: api._MemoryPackageMetadata(1, "3.0.14.0"),
    )
    monkeypatch.setattr(api, "_published_running_version", lambda: "3.0.14")
    monkeypatch.setattr(api, "get_build_identity", lambda: SimpleNamespace(kind="package"))
    monkeypatch.setattr(api, "probe_memory_runtime_entrypoint", lambda: None)
    monkeypatch.setattr(
        api,
        "_memory_artifact_status",
        lambda **_: (True, {"installed": True, "status": "ready"}),
    )

    package, _runtime = api._memory_dependencies_status(offline=True)

    assert package["readiness"] == "ready"
    assert package["has_update"] is False


def test_memory_package_metadata_enumerates_canonical_provider_set(
    monkeypatch,
) -> None:
    import importlib.metadata

    calls: list[str] = []
    providers = (SimpleNamespace(version="3.0.14"), SimpleNamespace(version="3.0.14"))

    def distributions(*, name: str):
        calls.append(name)
        return providers

    monkeypatch.setattr(importlib.metadata, "distributions", distributions)

    assert api._inspect_memory_package_metadata() == api._MemoryPackageMetadata(2, None)
    assert calls == ["avibe-memory"]


def test_missing_first_run_config_is_readable_not_required(monkeypatch) -> None:
    monkeypatch.setattr(
        api.V2Config,
        "load",
        classmethod(lambda _cls: (_ for _ in ()).throw(FileNotFoundError())),
    )

    requirement = api._load_memory_requirement()

    assert requirement == api._MemoryRequirementProjection(False, "not_required")


@pytest.mark.parametrize(
    "case",
    ("disabled", "safe-degraded-memory", "whole-config-failure"),
)
def test_memory_indep_021_status_import_fence(tmp_path, case) -> None:
    script = r'''
import importlib.abc
import json
import sys
from types import SimpleNamespace

from config import paths
from config.v2_config import V2Config

case = sys.argv[1]
config_path = paths.get_config_path()
config_path.parent.mkdir(parents=True, exist_ok=True)
config = V2Config.default()
config.save(config_path)
if case == "safe-degraded-memory":
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["memory"]["recovery_intent"] = "invalid"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
elif case == "whole-config-failure":
    config_path.write_text("{", encoding="utf-8")

assert not any(
    name == "avibe_memory" or name.startswith("avibe_memory.")
    for name in sys.modules
)

class BlockMemoryImplementation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "avibe_memory" or fullname.startswith("avibe_memory."):
            raise AssertionError(f"optional implementation import: {fullname}")
        return None

sys.meta_path.insert(0, BlockMemoryImplementation())
from vibe import api

metadata_calls = []

def inspect_metadata():
    metadata_calls.append(True)
    return api._MemoryPackageMetadata(0, None)

api._inspect_memory_package_metadata = inspect_metadata
api._published_running_version = lambda: "3.0.14"
api.get_build_identity = lambda: SimpleNamespace(kind="package")
package, runtime = api._memory_dependencies_status(offline=True)
if case == "whole-config-failure":
    assert package["readiness"] == "memory_requirement_unreadable"
    assert package["reason"] == "memory_requirement_unreadable"
    assert package["action_class"] == "operator_only"
    assert metadata_calls == []
else:
    assert package["readiness"] == "not_required"
    assert package["status"] == "missing"
    assert package["reason"] == "memory_package_missing"
    assert package["action_class"] == "repairable"
    assert runtime["status"] == "not_required"
    assert metadata_calls == [True]
    if case == "safe-degraded-memory":
        assert package["warnings"]

from vibe.upgrade import MemoryRequirementUnreadableError, configured_memory_enabled

if case == "whole-config-failure":
    try:
        configured_memory_enabled()
    except MemoryRequirementUnreadableError:
        pass
    else:
        raise AssertionError("whole-config failure admitted package mutation")
else:
    assert configured_memory_enabled() is False

assert not any(
    name == "avibe_memory" or name.startswith("avibe_memory.")
    for name in sys.modules
)
'''
    env = {
        **os.environ,
        "AVIBE_HOME": str(tmp_path / case),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    result = subprocess.run(
        [sys.executable, "-c", script, case],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
            return {
                "install": {"state": "absent"},
                "manifest": None,
                "node_available": True,
                "node_supported": False,
                "node_version": "16.0",
            }

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())
    by = {d["id"]: d for d in api.dependencies_status()["deps"]}
    assert by["node"]["installed"] is False and by["node"]["status"] == "missing"


def test_reconcile_startup_dependencies_uses_automatic_runtime_admission(monkeypatch):
    askill_calls = []
    avault_calls = []
    memory_calls = []

    def reconcile_memory_package():
        memory_calls.append("reconcile")
        return {"ok": True, "skipped": True, "reason": "memory_not_required"}

    monkeypatch.setattr(api, "reconcile_memory_package_on_startup", reconcile_memory_package)

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
    assert memory_calls == ["reconcile"]
    assert out["memory_package"]["reason"] == "memory_not_required"
    assert askill_calls == [False]
    assert avault_calls == [False]
    assert manager.prepared == [(False, True)]
    assert out["node"]["status"] == "ready"
    assert out["show_runtime"]["ok"] is True
    assert out["show_runtime"]["status"] == "pending_prewarm"
    assert out["show_runtime"]["policy"]["state"] == "allowed"
    assert out["show_runtime"]["install"]["state"] == "installed"
    assert out["show_runtime"]["runtime"]["state"] == "unchecked"


def test_memory_indep_026_startup_repairs_required_missing_companion_once(monkeypatch):
    """MEMORY-INDEP-026: a split-first startup converges through exact repair."""

    calls: list[bool] = []
    monkeypatch.setattr(
        api,
        "_memory_dependencies_status",
        lambda *, offline: (
            {
                "required": True,
                "readiness": "not_ready",
                "reason": "memory_package_missing",
                "action_class": "repairable",
            },
            {},
        ),
    )

    def repair(*, automatic: bool = False) -> dict:
        assert automatic is True
        calls.append(True)
        return {
            "ok": True,
            "message": "memory_package_ready",
            "reason": None,
            "restarting": True,
            "restart": {"job_id": "restart"},
        }

    monkeypatch.setattr(api, "_prepare_memory_package_job", repair)

    result = api.reconcile_memory_package_on_startup()

    assert calls == [True]
    assert result == {
        "ok": True,
        "message": "memory_package_ready",
        "reason": None,
        "restarting": True,
        "restart": {"job_id": "restart"},
    }


def test_startup_memory_repair_continues_other_dependencies_before_service_restart(
    monkeypatch,
):
    events: list[str] = []
    monkeypatch.setattr(
        api,
        "reconcile_memory_package_on_startup",
        lambda: {
            "ok": True,
            "message": "memory_package_ready",
            "reason": None,
            "restarting": True,
        },
    )
    monkeypatch.setattr(
        api,
        "ensure_askill_installed",
        lambda **_kwargs: events.append("askill") or {"ok": True},
    )
    monkeypatch.setattr(
        api,
        "ensure_avault_installed",
        lambda **_kwargs: events.append("avault") or {"ok": True},
    )
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "0")

    import core.show_runtime as srt_mod

    class _Mgr:
        def status(self, *, offline=False):
            return {
                "node_available": False,
                "node_supported": None,
                "node_version": None,
            }

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())

    result = api.reconcile_startup_dependencies()

    assert events == ["askill", "avault"]
    assert result["memory_package"]["restarting"] is True
    assert result["show_runtime"]["status"] == "failed"


def test_memory_indep_028_startup_retries_after_restart_admission(monkeypatch):
    """MEMORY-INDEP-028: restart admission cannot strand the companion."""

    events: list[str] = []
    memory_results = iter(
        (
            {
                "ok": False,
                "message": "memory_package_upgrade_busy",
                "reason": "memory_package_upgrade_busy",
            },
            {
                "ok": True,
                "message": "memory_package_ready",
                "reason": None,
                "restarting": True,
            },
        )
    )

    def reconcile_memory_package() -> dict:
        result = next(memory_results)
        events.append(str(result["message"]))
        return result

    monkeypatch.setattr(
        api,
        "reconcile_memory_package_on_startup",
        reconcile_memory_package,
    )
    monkeypatch.setattr(
        api,
        "ensure_askill_installed",
        lambda **_kwargs: events.append("askill") or {"ok": True},
    )
    monkeypatch.setattr(
        api,
        "ensure_avault_installed",
        lambda **_kwargs: events.append("avault") or {"ok": True},
    )
    restart_pending = iter((True, False))
    monkeypatch.setattr(api, "restart_is_pending", lambda: next(restart_pending))
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: events.append("wait"))
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "0")

    import core.show_runtime as srt_mod

    class _Mgr:
        def status(self, *, offline=False):
            return {
                "node_available": False,
                "node_supported": None,
                "node_version": None,
            }

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())

    result = api.reconcile_startup_dependencies()

    assert events == [
        "memory_package_upgrade_busy",
        "askill",
        "avault",
        "wait",
        "memory_package_ready",
    ]
    assert result["memory_package"]["message"] == "memory_package_ready"


def test_startup_memory_retry_stays_bounded_while_restart_remains_pending(
    monkeypatch,
):
    busy = {
        "ok": False,
        "message": "memory_package_upgrade_busy",
        "reason": "memory_package_upgrade_busy",
    }
    monotonic = iter((0.0, 0.0, 0.6))
    sleeps: list[float] = []
    monkeypatch.setattr(api, "restart_is_pending", lambda: True)
    monkeypatch.setattr(api.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(api.time, "sleep", sleeps.append)
    monkeypatch.setattr(api, "_STARTUP_MEMORY_PACKAGE_RETRY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(api, "_STARTUP_MEMORY_PACKAGE_RETRY_INTERVAL_SECONDS", 0.25)
    monkeypatch.setattr(
        api,
        "_reconcile_startup_memory_package_guarded",
        lambda: pytest.fail("repair must wait until restart admission is released"),
    )

    result = api._retry_startup_memory_package_after_restart(busy)

    assert result == busy
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    "package",
    (
        {
            "required": False,
            "readiness": "not_required",
            "reason": None,
            "action_class": "none",
        },
        {
            "required": True,
            "readiness": "ready",
            "reason": None,
            "action_class": "none",
        },
        {
            "required": True,
            "readiness": "not_ready",
            "reason": "memory_package_source_build",
            "action_class": "operator_only",
        },
    ),
    ids=("disabled", "ready", "source"),
)
def test_startup_memory_package_reconcile_skips_nonrepairable_states(monkeypatch, package):
    monkeypatch.setattr(
        api,
        "_memory_dependencies_status",
        lambda *, offline: (package, {}),
    )
    monkeypatch.setattr(
        api,
        "_prepare_memory_package_job",
        lambda: pytest.fail("a non-repairable package state must not install"),
    )

    result = api.reconcile_memory_package_on_startup()

    assert result["ok"] is True
    assert result["skipped"] is True


def test_startup_memory_repair_failure_keeps_other_dependency_reconcile_running(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        api,
        "reconcile_memory_package_on_startup",
        lambda: {
            "ok": False,
            "message": "memory_package_install_failed",
            "reason": "memory_package_install_failed",
        },
    )
    monkeypatch.setattr(
        api,
        "ensure_askill_installed",
        lambda *, force: events.append("askill") or {"ok": True},
    )
    monkeypatch.setattr(
        api,
        "ensure_avault_installed",
        lambda *, force: events.append("avault") or {"ok": True},
    )

    import core.show_runtime as srt_mod

    class _Mgr:
        def status(self, *, offline=False):
            return {"node_available": False, "node_supported": None, "node_version": None}

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "0")

    result = api.reconcile_startup_dependencies()

    assert events == ["askill", "avault"]
    assert result["ok"] is False
    assert result["memory_package"]["reason"] == "memory_package_install_failed"


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


@pytest.mark.parametrize(
    ("package", "expected_reason"),
    (
        pytest.param(
            {
                "required": False,
                "provider_count": 0,
                "version": None,
                "reason": None,
                "action_class": "none",
            },
            "memory_not_required",
            id="not-required",
        ),
        pytest.param(
            {
                "required": True,
                "provider_count": None,
                "version": None,
                "reason": "memory_package_metadata_unreadable",
                "action_class": "operator_only",
            },
            "memory_package_metadata_unreadable",
            id="operator-only-metadata",
        ),
        pytest.param(
            {
                "required": True,
                "provider_count": 1,
                "version": "3.0.14",
                "reason": "memory_package_source_build",
                "action_class": "operator_only",
            },
            "memory_package_source_build",
            id="source-deployment",
        ),
        pytest.param(
            {
                "required": True,
                "provider_count": 1,
                "version": "3.0.14",
                "reason": None,
                "action_class": "none",
            },
            "memory_package_not_repairable",
            id="already-ready",
        ),
    ),
)
def test_memory_package_server_admission_rejects_nonrepairable_rows(
    monkeypatch,
    package,
    expected_reason,
) -> None:
    monkeypatch.setattr(
        api,
        "_memory_dependencies_status",
        lambda **_: (package, {"id": "memory-runtime"}),
    )

    result = api.start_dependency_install_job("memory-package")

    assert result == {
        "ok": False,
        "status": "rejected",
        "message": expected_reason,
        "output": None,
        "reason": expected_reason,
        "action_class": "operator_only",
    }


#: One running version, published two ways. `publish.yml` accepts official
#: `vX.Y.ZrcN` tags and publishes them to PyPI, while a `gh-v*` build of the
#: identical version is on no index at all — so the version string cannot pick
#: the repair sources and only the recorded install origin can. Both rows use
#: the same version deliberately.
REPAIR_VERSION = "3.0.14rc8"
RELEASE_CORE_URL = f"https://github.com/avibe-bot/avibe/releases/download/gh-v{REPAIR_VERSION}/avibe_os-{REPAIR_VERSION}-py3-none-any.whl"
RELEASE_MEMORY_URL = (
    f"https://github.com/avibe-bot/avibe/releases/download/gh-v{REPAIR_VERSION}/"
    f"avibe_memory-{REPAIR_VERSION}-py3-none-any.whl"
)
INSTALL_ORIGIN_SOURCES = {
    "index install": (None, None, None),
    "release asset install": (RELEASE_CORE_URL, RELEASE_CORE_URL, RELEASE_MEMORY_URL),
}


@pytest.mark.parametrize(
    ("origin", "core_spec", "memory_spec"),
    list(INSTALL_ORIGIN_SOURCES.values()),
    ids=list(INSTALL_ORIGIN_SOURCES),
)
def test_memory_package_dependency_job_targets_the_running_version_wherever_it_came_from(
    monkeypatch,
    origin: str | None,
    core_spec: str | None,
    memory_spec: str | None,
) -> None:
    current_version = REPAIR_VERSION
    monkeypatch.setattr("vibe.upgrade._recorded_install_origin", lambda _package: origin)
    plan = SimpleNamespace(
        command=["repair"],
        activation=None,
        method="pip",
        preflight_error=None,
    )
    calls: dict[str, object] = {}
    lock_events: list[str] = []
    monkeypatch.setattr(api, "_memory_package_repair_rejection", lambda **_kwargs: None)
    monkeypatch.setattr(api, "_published_running_version", lambda: current_version)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/bin/vibe")
    monkeypatch.setattr(api, "get_safe_cwd", lambda: "/safe")
    monkeypatch.setattr(api, "restart_is_pending", lambda: False)
    monkeypatch.setattr(api, "_memory_package_restart_retry_required", lambda _version: False)
    record_result = Mock()
    monkeypatch.setattr(api, "_record_memory_package_repair_result", record_result)
    monkeypatch.setattr(
        api,
        "verify_python_environment",
        lambda _python: SimpleNamespace(ok=True, detail="ok"),
    )

    class _Lock:
        def __enter__(self):
            lock_events.append("entered")

        def __exit__(self, *_args):
            lock_events.append("exited")

    monkeypatch.setattr(api, "atomic_upgrade_lock", _Lock)

    def build_plan(**kwargs):
        lock_events.append("build")
        calls["plan"] = kwargs
        return plan

    def execute_plan(actual, **kwargs):
        lock_events.append("execute")
        calls["execute"] = (actual, kwargs)
        return subprocess.CompletedProcess(["repair"], 0, stdout="installed", stderr="")

    monkeypatch.setattr(api, "build_upgrade_plan", build_plan)
    monkeypatch.setattr(api, "execute_upgrade_plan", execute_plan)
    def schedule_restart(**kwargs):
        lock_events.append("restart")
        calls["restart"] = kwargs
        return {"job_id": "restart"}

    monkeypatch.setattr(api, "schedule_restart", schedule_restart)

    result = api._prepare_memory_package_job()

    assert result["ok"] is True
    assert result["message"] == "memory_package_ready"
    assert calls["plan"] == {
        "version": current_version,
        "package_name": api.PACKAGE_NAME,
        "memory_package": True,
        "memory_version": current_version,
        "vibe_path": "/bin/vibe",
        "core_spec": core_spec,
        "memory_spec": memory_spec,
    }
    assert calls["execute"] == (
        plan,
        {
            "run": subprocess.run,
            "capture_output": True,
            "text": True,
            "timeout": api.UPGRADE_INSTALL_TIMEOUT_SECONDS,
            "cwd": "/safe",
        },
    )
    assert calls["restart"] == {
        "delay_seconds": 2.0,
        "vibe_path": "/bin/vibe",
        "trigger": "memory-package-repair",
        "scope": "service",
    }
    assert result["restarting"] is True
    assert result["restart"] == {"job_id": "restart"}
    assert lock_events == ["entered", "build", "execute", "restart", "exited"]
    record_result.assert_called_once_with(
        current_version,
        result="restart_scheduled",
        reason=None,
    )


def test_memory_indep_027_preview_repair_installs_the_release_that_published_it(
    monkeypatch,
) -> None:
    """MEMORY-INDEP-027: a core-only preview install converges from its own release.

    A `gh-v*` release publishes the wheel pair as release assets and nothing to
    an index, so this builds the real plan rather than a recorded call: what
    matters is the command an installer would actually run.
    """

    current_version = REPAIR_VERSION
    calls: dict[str, object] = {}
    monkeypatch.setattr("vibe.upgrade._recorded_install_origin", lambda _package: RELEASE_CORE_URL)
    monkeypatch.setattr(api, "_memory_package_repair_rejection", lambda **_kwargs: None)
    monkeypatch.setattr(api, "_published_running_version", lambda: current_version)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/bin/vibe")
    monkeypatch.setattr(api, "get_safe_cwd", lambda: "/safe")
    monkeypatch.setattr(api, "restart_is_pending", lambda: False)
    monkeypatch.setattr(api, "_memory_package_restart_retry_required", lambda _version: False)
    monkeypatch.setattr(api, "_record_memory_package_repair_result", Mock())
    monkeypatch.setattr(api, "atomic_upgrade_lock", nullcontext)
    monkeypatch.setattr(api, "schedule_restart", lambda **_kwargs: {"job_id": "restart"})
    monkeypatch.setattr(
        api,
        "verify_python_environment",
        lambda _python: SimpleNamespace(ok=True, detail="ok"),
    )
    # Resolve to the pip installer so the plan is built from this interpreter
    # without consulting any uv tool environment on the host.
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **_kwargs: None)

    def execute_plan(plan, **_kwargs):
        calls["plan"] = plan
        return subprocess.CompletedProcess(plan.command, 0, stdout="installed", stderr="")

    monkeypatch.setattr(api, "execute_upgrade_plan", execute_plan)

    result = api._prepare_memory_package_job(automatic=True)

    assert result["ok"] is True
    assert result["restarting"] is True
    plan = calls["plan"]
    release = f"https://github.com/avibe-bot/avibe/releases/download/gh-v{current_version}/"
    commands = [
        command
        for command in (plan.command, plan.preflight_command, plan.preflight_fallback_command)
        if command
    ]
    assert len(commands) >= 2, "the repair resolves the pair before it installs it"
    for command in commands:
        assert f"{release}avibe_os-{current_version}-py3-none-any.whl" in command
        assert f"avibe-memory @ {release}avibe_memory-{current_version}-py3-none-any.whl" in command
        # An index pin here is the bug: PyPI never served this version, so the
        # install fails and spends one of the bounded repair attempts.
        assert f"{api.PACKAGE_NAME}=={current_version}" not in command
        assert f"{api.MEMORY_PACKAGE_NAME}=={current_version}" not in command


def test_memory_package_dependency_job_fails_closed_when_restart_cannot_be_scheduled(
    monkeypatch,
    tmp_path,
) -> None:
    state_path = tmp_path / "memory-package-auto-repair.json"
    monkeypatch.setattr(api, "_memory_package_auto_repair_state_path", lambda: state_path)
    monkeypatch.setattr(api, "_memory_package_repair_rejection", lambda **_kwargs: None)
    monkeypatch.setattr(api, "_published_running_version", lambda: "3.0.14")
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/bin/vibe")
    monkeypatch.setattr(api, "get_safe_cwd", lambda: "/safe")
    monkeypatch.setattr(api, "atomic_upgrade_lock", nullcontext)
    monkeypatch.setattr(api, "restart_is_pending", lambda: False)
    monkeypatch.setattr(
        api,
        "verify_python_environment",
        lambda _python: SimpleNamespace(ok=True, detail="ok"),
    )
    build_plan = Mock(
        return_value=SimpleNamespace(
            activation=None,
            method="pip",
            preflight_error=None,
        )
    )
    monkeypatch.setattr(
        api,
        "build_upgrade_plan",
        build_plan,
    )
    execute_plan = Mock(
        return_value=subprocess.CompletedProcess(
            ["repair"], 0, stdout="installed", stderr=""
        )
    )
    monkeypatch.setattr(
        api,
        "execute_upgrade_plan",
        execute_plan,
    )
    schedule = Mock(side_effect=RuntimeError("restart unavailable"))
    monkeypatch.setattr(api, "schedule_restart", schedule)

    result = api._prepare_memory_package_job()

    assert result == {
        "ok": False,
        "message": "memory_package_restart_failed",
        "output": "installed",
        "reason": "memory_package_restart_failed",
        "restarting": False,
    }
    assert api._memory_package_restart_retry_required("3.0.14") is True

    schedule.side_effect = None
    schedule.return_value = {"job_id": "restart"}
    retry = api._prepare_memory_package_job()

    assert retry["ok"] is True
    assert retry["restarting"] is True
    assert build_plan.call_count == 1
    assert execute_plan.call_count == 1
    assert schedule.call_count == 2
    assert api._memory_package_restart_retry_required("3.0.14") is False


def test_memory_indep_026_auto_repair_persists_a_per_version_attempt_budget(
    monkeypatch,
    tmp_path,
) -> None:
    """MEMORY-INDEP-026: restart loops stop after the persisted attempt budget."""

    state_path = tmp_path / "memory-package-auto-repair.json"
    installs: list[int] = []
    restarts: list[int] = []
    monkeypatch.setattr(api, "_memory_package_auto_repair_state_path", lambda: state_path)
    monkeypatch.setattr(api, "atomic_upgrade_lock", nullcontext)
    monkeypatch.setattr(api, "_memory_package_repair_rejection", lambda **_kwargs: None)
    monkeypatch.setattr(api, "_published_running_version", lambda: "3.0.14")
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/bin/vibe")
    monkeypatch.setattr(api, "get_safe_cwd", lambda: "/safe")
    monkeypatch.setattr(api, "restart_is_pending", lambda: False)
    monkeypatch.setattr(
        api,
        "build_upgrade_plan",
        lambda **_kwargs: SimpleNamespace(
            activation=None,
            method="pip",
            preflight_error=None,
        ),
    )
    monkeypatch.setattr(
        api,
        "execute_upgrade_plan",
        lambda *_args, **_kwargs: installs.append(1)
        or subprocess.CompletedProcess(["repair"], 0, stdout="installed", stderr=""),
    )
    monkeypatch.setattr(
        api,
        "verify_python_environment",
        lambda _python: SimpleNamespace(ok=True, detail="ok"),
    )
    monkeypatch.setattr(
        api,
        "schedule_restart",
        lambda **_kwargs: restarts.append(1) or {"job_id": f"restart-{len(restarts)}"},
    )

    results = [api._prepare_memory_package_job(automatic=True) for _ in range(4)]

    assert len(installs) == len(restarts) == api._MEMORY_PACKAGE_AUTO_REPAIR_MAX_ATTEMPTS
    assert all(result.get("restarting") for result in results[:3])
    assert results[3]["skipped"] is True
    assert results[3]["reason"] == "memory_package_auto_repair_exhausted"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["core_version"] == "3.0.14"
    assert state["attempts"] == 3
    assert state["result"] == "restart_scheduled"


def test_memory_auto_repair_budget_resets_only_for_a_new_core_version(monkeypatch, tmp_path) -> None:
    state_path = tmp_path / "memory-package-auto-repair.json"
    monkeypatch.setattr(api, "_memory_package_auto_repair_state_path", lambda: state_path)

    first = api._reserve_memory_package_auto_repair_attempt("3.0.14")
    api._finish_memory_package_auto_repair_attempt(
        "3.0.14",
        first["token"],
        result="failed",
        reason="memory_package_install_failed",
    )
    second = api._reserve_memory_package_auto_repair_attempt("3.0.15")

    assert first["attempts"] == 1
    assert second["attempts"] == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["core_version"] == "3.0.15"
    assert state["result"] == "running"


def test_memory_package_reachability_disabled_enable_bootstrap_ready(
    monkeypatch,
    tmp_path,
) -> None:
    import time as _t

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    api.V2Config.default().save()
    metadata = {"value": api._MemoryPackageMetadata(0, None)}
    monkeypatch.setattr(api, "_inspect_memory_package_metadata", lambda: metadata["value"])
    monkeypatch.setattr(api, "_published_running_version", lambda: "3.0.14")
    monkeypatch.setattr(api, "get_build_identity", lambda: SimpleNamespace(kind="package"))
    monkeypatch.setattr(api, "probe_memory_runtime_entrypoint", lambda: None)
    monkeypatch.setattr(
        api,
        "_memory_artifact_status",
        lambda **_: (True, {"installed": True, "status": "ready", "matches_manifest": True}),
    )

    disabled, _runtime = api._memory_dependencies_status(offline=True)
    assert (disabled["required"], disabled["status"], disabled["action_class"]) == (
        False,
        "missing",
        "repairable",
    )
    monkeypatch.setattr(
        api,
        "_prepare_memory_package_job",
        lambda **_kwargs: pytest.fail("disabled startup must not auto-install"),
    )
    assert api.reconcile_memory_package_on_startup()["skipped"] is True

    def bootstrap() -> dict:
        metadata["value"] = api._MemoryPackageMetadata(1, "3.0.14")
        return {"ok": True, "message": "memory_package_ready", "output": None}

    monkeypatch.setattr(api, "_prepare_memory_package_job", bootstrap)
    with api._AGENT_INSTALL_JOB_LOCK:
        api._AGENT_INSTALL_JOBS.clear()
        api._AGENT_INSTALL_LATEST_BY_BACKEND.clear()
    started = api.start_dependency_install_job("memory-package")
    result = started
    for _ in range(100):
        result = api.get_agent_install_job(started["job_id"], backend="memory-package")
        if result.get("status") != "running":
            break
        _t.sleep(0.01)
    assert result["status"] == "succeeded"

    optional_ready, _runtime = api._memory_dependencies_status(offline=True)
    assert (optional_ready["readiness"], optional_ready["action_class"]) == (
        "not_required",
        "repairable",
    )
    assert api._memory_package_repair_rejection(allow_optional=True) is None
    monkeypatch.setattr(
        api,
        "_prepare_memory_package_job",
        lambda **_kwargs: pytest.fail("disabled startup must remain a no-op"),
    )
    assert api.reconcile_memory_package_on_startup()["skipped"] is True

    api.save_memory_config(
        {
            "enabled": True,
            "mode": "custom",
            "processing": {
                "llm": {
                    "base_url": "https://llm.example.test/v1",
                    "model": "chat",
                    "api_key": "test-key",
                },
                "embedding": {
                    "base_url": "https://embedding.example.test/v1",
                    "model": "embedding",
                    "api_key": "test-key",
                },
            },
        }
    )
    ready, _runtime = api._memory_dependencies_status(offline=True)
    assert (ready["readiness"], ready["action_class"]) == (
        "ready",
        "none",
    )


def test_start_dependency_install_job_rejects_unknown():
    assert api.start_dependency_install_job("bogus")["ok"] is False


def test_prepare_show_runtime_job_surfaces_retry_diagnostics(monkeypatch):
    import core.show_runtime as show_runtime

    manager = Mock()
    manager.repair.return_value = {
        "ok": False,
        "outcome": "failed",
        "reason": "runtime_archive_download_failed",
        "download_error": {
            "kind": "timeout",
            "message": "Connection timed out",
            "url": "https://example.test/runtime.tgz",
            "retryable": True,
            "attempts": 3,
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
    manager.repair.return_value = {
        "ok": False,
        "outcome": "failed",
        "reason": "runtime_archive_download_failed",
        "installed": True,
        "command": ["node", "runtime-cli.js"],
    }
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)

    result = api._prepare_show_runtime_job()

    assert result["ok"] is False
    assert result["reason"] == "runtime_archive_download_failed"
    assert "runtime_archive_download_failed" in result["message"]


def test_prepare_show_runtime_job_reports_healthy_runtime_without_change(monkeypatch):
    import core.show_runtime as show_runtime

    manager = Mock()
    manager.repair.return_value = {"ok": True, "outcome": "healthy"}
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)

    result = api._prepare_show_runtime_job()

    assert result == {
        "ok": True,
        "message": "Show Runtime starts successfully; no repair is needed.",
        "output": None,
        "outcome": "healthy",
        "changed": False,
    }


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
