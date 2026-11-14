"""Host-side checks for the hosted native runner, not Windows execution evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "desktop/scripts/test-windows-driver-source.ps1"


def test_native_runner_applies_full_patch_before_supplement(monkeypatch, tmp_path):
    script = RUNNER.read_text()
    # Execute the embedded Python without depending on its PowerShell binding name.
    match = re.search(r"@'\n(from __future__ import annotations\n.*?)\n'@", script, re.S)
    assert match
    helper = compile(match[1], "native-source-helper", "exec")
    manifest = json.loads((ROOT / "desktop/cua-driver/sources.json").read_text())
    archive = tmp_path / "source.tar.gz"
    archive_bytes = b"test-owned source archive"
    manifest["driver"]["source"]["sha256"] = hashlib.sha256(archive_bytes).hexdigest()
    patch1 = tmp_path / "0001.patch"
    patch2 = tmp_path / "0002.patch"
    patch1.write_bytes(b"full first patch")
    patch2.write_bytes(b"Windows supplement")
    manifest["driver"]["patch"]["sha256"] = hashlib.sha256(patch1.read_bytes()).hexdigest()
    manifest_path = tmp_path / "sources.json"
    manifest_path.write_text(json.dumps(manifest))
    destination = tmp_path / "source"
    events = []

    def download(url, path):
        assert url == manifest["driver"]["source"]["url"]
        path.write_bytes(archive_bytes)

    def extract_driver_source(selected, path, target):
        assert selected == manifest
        assert path == archive and target == destination
        events.append("extract")
        return target

    def apply_driver_patch(selected, target):
        assert target == destination
        events.append(selected["driver"]["patch"]["file"])

    monkeypatch.setattr(
        runpy,
        "run_path",
        lambda _: {
            "download": download,
            "extract_driver_source": extract_driver_source,
            "apply_driver_patch": apply_driver_patch,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["helper", "prepare.py", str(manifest_path), str(patch1), str(patch2), str(archive), str(destination)],
    )
    exec(helper, {})
    assert events == ["extract", manifest["driver"]["patch"]["file"], "patches/0002-windows-security-boundary.patch"]
    events.clear()
    patch1.write_bytes(b"modified first patch")
    with pytest.raises(ValueError, match="0001 patch hash"):
        exec(helper, {})
    assert events == []


def test_production_capture_uses_isolated_direct_mcp(monkeypatch, tmp_path):
    capture = runpy.run_path(str(ROOT / "desktop/scripts/capture-windows-cua-driver.py"))
    calls = []
    response = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "click"}]}}

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["env"]["CUA_DRIVER_MCP_ENVELOPES"] == "0"
        assert kwargs["env"]["CUA_DRIVER_PERMISSION_MODE"] == "standard"
        assert kwargs["env"]["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "false"
        assert kwargs["env"]["HOME"] != os.environ.get("HOME")
        assert b'"method":"tools/list"' in kwargs["input"]
        return SimpleNamespace(
            returncode=0,
            stdout=(json.dumps({"id": 1, "result": {}}) + "\n" + json.dumps(response) + "\n").encode(),
            stderr=b"",
        )

    monkeypatch.setattr(capture["subprocess"], "run", fake_run)
    result, raw, stderr = capture["run_tools_list"](tmp_path / "cua-driver.exe", timeout=5)
    assert result == response
    assert raw.endswith(b"\n")
    assert stderr == b""
    assert calls[0][0] == [str(tmp_path / "cua-driver.exe"), "mcp", "--direct"]


def test_production_capture_is_wired_into_the_existing_windows_job():
    workflow = yaml.safe_load((ROOT / ".github/workflows/desktop-shell.yml").read_text())
    steps = workflow["jobs"]["shell"]["steps"]
    source = next(step for step in steps if step.get("name") == "Verify Windows Cua Driver security boundary as ordinary user")
    assert "-CaptureProductionDriver" in source["run"]
    upload = next(step for step in steps if step.get("with", {}).get("name", "").startswith("windows-cua-driver-production-"))
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["path"].strip().endswith("production/**")


def test_windows_manifest_records_ordered_final_patch():
    manifest = json.loads((ROOT / "desktop/cua-driver/sources.json").read_text())
    windows = manifest["driver"]["windows"]
    assert windows["patch_order"] == [
        "patches/0001-macos-raw-single-click.patch",
        "patches/0002-windows-security-boundary.patch",
    ]
    assert windows["patch"]["sha256"] == "9a8ab6af58de18fa85dc268f22da9e60f528e798f9386b4ead6f3127f6f80e9f"


def test_native_evidence_upload_is_bounded():
    # Environment/profile, exact selectors and XML validation are exercised by
    # the native job. Do not pretend that finding their implementation spelling
    # in a script proves they execute. This check owns only the published
    # artifact-name/path contract in the parsed workflow configuration.
    workflow = yaml.safe_load((ROOT / ".github/workflows/desktop-shell.yml").read_text())
    steps = workflow["jobs"]["shell"]["steps"]
    upload = next(
        step for step in steps if step.get("with", {}).get("name", "").startswith("windows-cua-driver-security-")
    )
    assert upload["with"]["if-no-files-found"] == "error"
    paths = upload["with"]["path"].splitlines()
    assert len(paths) == 3
    assert [path.rsplit("*/", 1)[-1] for path in paths] == [
        "provenance.json",
        "junit-*.xml",
        "logs/**",
    ]
