"""Network failures give explicit recovery steps without switching live installs."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibe import api, cli, release_dependencies, restart_supervisor, upgrade

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("method,index", [("uv", "UV_DEFAULT_INDEX"), ("pip", "PIP_INDEX_URL")])
@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("windows", [False, True])
def test_network_advice_is_exact_localized_and_value_free(monkeypatch, method, index, language, windows):
    monkeypatch.setattr(upgrade, "os", SimpleNamespace(name="nt" if windows else "posix"))
    monkeypatch.setenv("HTTPS_PROXY", "http://user:do-not-print@proxy.invalid:3128")
    monkeypatch.setenv(index, "https://user:do-not-print@index.invalid/simple")
    advice = upgrade.upgrade_download_help(method, language)
    assert "HTTPS_PROXY" in advice and index in advice
    assert "vibe upgrade" in advice
    assert "do-not-print" not in advice
    assert "(including in mainland China)" in advice if language == "en" else "中国大陆" in advice
    assert ('$env:HTTPS_PROXY="' in advice) is windows
    assert (f'$env:{index}="' in advice) is windows


@pytest.mark.parametrize("surface", ["cli", "api"])
@pytest.mark.parametrize("failure", ["metadata", "dependency", "timeout"])
def test_failed_upgrade_keeps_running_process_and_old_launcher(monkeypatch, tmp_path, capsys, surface, failure):
    root = tmp_path / "generations"
    old = root / "old" / "bin" / "vibe"
    candidate = root / "candidate" / "bin" / "vibe"
    launcher = tmp_path / "bin" / "vibe"
    for path in (old, candidate, launcher):
        path.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("old executable\n", encoding="utf-8")
    candidate.write_text("partial candidate\n", encoding="utf-8")
    launcher.symlink_to(old)
    plan = upgrade.UpgradePlan(
        ["uv", "tool", "install", "avibe-os"], {}, "uv",
        activation=upgrade.AtomicActivation(launcher, candidate, root / "old"), release_spec_index=3,
    )
    monkeypatch.setattr(upgrade, "atomic_uv_install_root", lambda: root)
    monkeypatch.setattr(upgrade, "build_upgrade_plan", lambda **_: plan)
    monkeypatch.setattr(api, "build_upgrade_plan", lambda **_: plan)
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: str(launcher))
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: str(launcher))
    version = {"error": None, "has_update": True, "latest": "3.2.6"}
    monkeypatch.setattr(cli, "get_latest_version", lambda: version)
    monkeypatch.setattr(api, "get_version_info", lambda: version)
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(api, "_configured_backend_language", lambda: "zh")

    def forbidden(*args, **kwargs):
        pytest.fail("a failed candidate must not activate, restart or prepare the running runtime")

    monkeypatch.setattr(upgrade, "activate_upgrade_candidate", forbidden)
    monkeypatch.setattr(api, "activate_upgrade_candidate", forbidden)
    monkeypatch.setattr(restart_supervisor, "schedule_restart", forbidden)
    monkeypatch.setattr(api, "schedule_restart", forbidden)
    monkeypatch.setattr(cli, "_prepare_show_runtime_after_install", forbidden)
    monkeypatch.setattr(api, "_prepare_show_runtime_after_upgrade", forbidden)
    calls = []

    def prepare(spec, destination):
        if failure == "metadata":
            raise OSError("PyPI and release mirror unavailable")
        return str(destination / "avibe_os-3.2.6-py3-none-any.whl"), None

    def install(command, **kwargs):
        calls.append(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1800)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="dependency download unavailable")

    monkeypatch.setattr(release_dependencies, "prepare_release", prepare)
    monkeypatch.setattr(subprocess, "run", install)
    # A process owned by this test represents a running service. Never start or
    # inspect the user's service; only this recorded child may be stopped.
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"])
    try:
        if surface == "cli":
            assert cli.cmd_upgrade() == 1
            output = capsys.readouterr().out
            assert "If PyPI" in output
        else:
            result = api.do_upgrade(auto_restart=True)
            assert result["ok"] is False and result["restarting"] is False
            output = result["output"] or ""
            assert "中国大陆" in output
        assert "HTTPS_PROXY" in output and "UV_DEFAULT_INDEX" in output
        assert launcher.resolve() == old.resolve()
        assert old.read_text(encoding="utf-8") == "old executable\n"
        assert not candidate.parent.parent.exists()
        assert child.poll() is None
        assert bool(calls) is (failure != "metadata")
    finally:
        child.terminate()
        child.wait(timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer; Windows exercises install.ps1 separately")
@pytest.mark.parametrize("custom", [False, True])
def test_shell_failure_shows_proxy_and_index_before_exiting(tmp_path, custom):
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    body = text.split("install_vibe() {", 1)[1].split("\ninstall_vibe_from_sources()", 1)[0]
    # Execute the real failure branch, while keeping installer/environment
    # mutation behind inert doubles. Test input is not interpolated into shell.
    script = '''
info() { :; }
warn() { printf '%s\\n' "$*"; }
error() { printf '%s\\n' "$*"; exit 1; }
choose_tool_bin_dir() { :; }
uv_python_install_mirror() { :; }
install_vibe_from_sources() { return 1; }
install_vibe() {''' + body + "\ninstall_vibe\n"
    env = {**os.environ, "AVIBE_INSTALL_PACKAGE_SPEC": "https://user:secret@private.example/pkg.whl" if custom else ""}
    result = subprocess.run(["bash", "-c", script], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert "export HTTPS_PROXY=http://127.0.0.1:7890" in result.stdout
    assert "export UV_DEFAULT_INDEX=https://your-trusted-index.example/simple" in result.stdout
    assert "Then rerun this installer" in result.stdout
    assert "secret" not in result.stdout
