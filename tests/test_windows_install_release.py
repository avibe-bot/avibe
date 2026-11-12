"""Exercise Windows install preparation/staging without touching a live service.

The shell installer tests miss PowerShell's independent source-selection flow.
Stub only external commands, keep the real shared resolver for local wheels,
then prove failures do not activate or mutate the previous launcher.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELLS = [shutil.which(name) for name in ("pwsh", "powershell") if shutil.which(name)]
SHELLS = SHELLS or [None]


def test_both_installers_embed_the_canonical_release_resolver():
    source = (ROOT / "vibe/release_dependencies.py").read_text(encoding="utf-8")
    for name in ("install.sh", "install.ps1"):
        text = (ROOT / name).read_text(encoding="utf-8")
        embedded = text.split("# BEGIN GENERATED RELEASE DEPENDENCIES\n", 1)[1].split(
            "# END GENERATED RELEASE DEPENDENCIES\n", 1)[0]
        assert embedded == source
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "pypi.tuna.tsinghua.edu.cn" not in powershell
    assert 'git+https://github.com/$REPO.git' not in powershell


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("case", ["download", "locked-dependency", "custom-source", "arm64", "python-cdn-retry"])
def test_powershell_failure_preserves_launcher_and_has_no_implicit_source_fallback(tmp_path, shell, case):
    if shell is None:
        pytest.skip("PowerShell is required for executable installer coverage")
    home = tmp_path / "home"
    home.mkdir()
    runtime = home / ".avibe"
    runtime.mkdir()
    stable = home / "stable bin"
    stable.mkdir()
    launcher = stable / "vibe.exe"
    launcher.write_bytes(b"previous launcher - must remain byte-identical")
    marker = stable / ".vibe.exe.avibe-generation"
    marker.write_text("previous generation")
    wheel = tmp_path / "avibe_os-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("avibe_os-9.9.9.dist-info/METADATA", "Name: avibe-os\nVersion: 9.9.9\n")
        archive.writestr("vibe/dependency_constraints.txt", "anyio==4.16.0\n")
    log = tmp_path / "commands.jsonl"
    source = (ROOT / "install.ps1").read_text(encoding="utf-8").split("\n# Each option means one thing", 1)[0]
    harness = r'''
$originalNative = ${function:Invoke-NativeCommand}
function Get-CimInstance { throw "No host hardware access in this test" }
function Get-UvPythonInstallMirror {
    if ($env:TEST_CASE -eq "python-cdn-retry") { return $ASTRAL_PYTHON_INSTALL_MIRROR }
    return $null
}
function Invoke-NativeCommand {
    param([string]$FilePath, [string[]]$Arguments)
    $record = @{ file = $FilePath; arguments = @($Arguments); mirror = $env:UV_PYTHON_INSTALL_MIRROR }
    if ($FilePath -eq "uv" -and $Arguments[0] -eq "tool") {
        $constraintAt = [array]::IndexOf($Arguments, "--constraints")
        if ($constraintAt -ge 0) {
            $record.constraints = [System.IO.File]::ReadAllText($Arguments[$constraintAt + 1])
        }
    }
    Add-Content -LiteralPath $env:TEST_LOG -Value ($record | ConvertTo-Json -Compress -Depth 5)
    if ($FilePath -eq "uv" -and $Arguments[0] -eq "run") {
        if ($env:TEST_CASE -in @("download", "python-cdn-retry")) {
            return @{ Success = $false; ExitCode = 1; Output = "https://secret-user:secret-password@unreachable.example" }
        }
        $helper = [array]::IndexOf($Arguments, "python") + 1
        return & $originalNative -FilePath $env:TEST_PYTHON -Arguments $Arguments[$helper..($Arguments.Length - 1)]
    }
    if ($FilePath -eq "uv" -and $Arguments[0] -eq "tool") {
        # The locked version cannot be downloaded. Staging exists, but the
        # real owner must discard it before any activation call can happen.
        return @{ Success = $false; ExitCode = 1; Output = "https://secret-user:secret-password@unreachable.example" }
    }
    if ($Arguments[0] -eq "__activate-install" -and $Arguments[1] -eq "--protocol-version") {
        return @{ Success = $false; ExitCode = 1; Stdout = ""; Output = "legacy launcher" }
    }
    throw "Unexpected command reached after failure"
}
Install-Vibe
'''
    script = tmp_path / "installer-test.ps1"
    script.write_text(source + harness, encoding="utf-8-sig")
    environment = {
        name: value for name, value in os.environ.items()
        if name.upper() not in {"PSMODULEPATH", "AVIBE_INSTALL_PACKAGE_SPEC", "VIBE_INSTALL_PACKAGE_SPEC",
                                "UV_PYTHON_INSTALL_MIRROR", "UV_PYTHON_DOWNLOADS_JSON_URL"}
    }
    environment.update({
        "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home / "appdata"),
        "AVIBE_HOME": str(runtime), "UV_TOOL_BIN_DIR": str(stable),
        "UV_TOOL_DIR": str(tmp_path / "original-tools"),
        "PROCESSOR_ARCHITECTURE": "ARM64" if case == "arm64" else "AMD64",
        "PROCESSOR_ARCHITEW6432": "",
        "TEST_CASE": case, "TEST_LOG": str(log), "TEST_PYTHON": sys.executable,
        "HTTPS_PROXY": "http://secret-user:secret-password@127.0.0.1:7890",
        "UV_DEFAULT_INDEX": "https://secret-user:secret-password@trusted.example/simple",
        "DOTNET_CLI_HOME": str(home),
    })
    if case not in {"download", "python-cdn-retry"}:
        environment["AVIBE_INSTALL_PACKAGE_SPEC"] = (
            "git+https://github.com/example/explicit-development.git" if case == "custom-source" else str(wheel)
        )
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=90,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert launcher.read_bytes() == b"previous launcher - must remain byte-identical"
    assert marker.read_text() == "previous generation"
    assert not list((runtime / "runtime" / "install-generations").glob("*"))
    assert 'secret-user' not in output and 'secret-password' not in output
    assert '$env:HTTPS_PROXY="http://127.0.0.1:7890"' in output
    assert '$env:UV_DEFAULT_INDEX="https://your-trusted-index.example/simple"' in output
    assert "before rerunning" in output and "actual proxy or trusted" in output
    commands = [json.loads(line.lstrip("\ufeff")) for line in log.read_text(encoding="utf-8-sig").splitlines()]
    preparation = [item for item in commands if item["arguments"][0] == "run"]
    installs = [item for item in commands if item["arguments"][:2] == ["tool", "install"]]
    assert len(preparation) == (2 if case == "python-cdn-retry" else 1)
    for item in preparation:
        assert item["arguments"][1] == "--no-project"
        assert "--no-config" not in item["arguments"]
        # All helper/snapshot output is temporary and removed even on failure.
        assert not Path(item["arguments"][-1]).exists()
    if case in {"download", "python-cdn-retry"}:
        assert not installs
        assert all(item["arguments"][-2] == "avibe-os" for item in preparation)
    else:
        assert len(installs) == 1
        if case == "custom-source":
            assert "--constraints" not in installs[0]["arguments"]
            assert "git+https://github.com/example/explicit-development.git" in installs[0]["arguments"]
        else:
            assert installs[0]["constraints"].splitlines() == ["anyio==4.16.0"]
            assert str(wheel) in installs[0]["arguments"]
    if case == "arm64":
        assert "cpython-3.12-windows-x86_64-none" in preparation[0]["arguments"]
        assert "cpython-3.12-windows-x86_64-none" in installs[0]["arguments"]
    if case == "python-cdn-retry":
        assert preparation[0]["mirror"] == "https://releases.astral.sh/github/python-build-standalone/releases/download"
        assert not preparation[1]["mirror"]
    assert not any(item["arguments"][0] in {"stop", "restart", "start"} for item in commands)
    assert not any("--candidate" in item["arguments"] for item in commands)
