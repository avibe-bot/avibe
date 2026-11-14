"""Host-side checks for the hosted native runner, not Windows execution evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import runpy
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "desktop/scripts/test-windows-driver-source.ps1"


def test_native_runner_applies_full_patch_before_supplement(monkeypatch, tmp_path):
    script = RUNNER.read_text()
    match = re.search(r"\$pythonHelper = @'\n(.*?)\n'@", script, re.S)
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


def test_native_runner_selectors_and_evidence_upload_are_bounded():
    script = RUNNER.read_text()
    selectors = re.findall(r"Selector = '([^']+)'", script)
    assert len(selectors) == len(set(selectors)) == 11
    assert sum(name.startswith("tool::") for name in selectors) == 6
    assert "launch_and_list_apps_share_installed_identity_rules" not in script
    assert "module['apply_driver_patch'](manifest, source_root)" in script
    assert "Verify-JunitEvidence" in script
    assert "$psi.LoadUserProfile = $true" in script
    assert "$psi.Environment.Clear()" in script
    assert "1 passed;\\s+0 failed;\\s+0 ignored;" in script
    workflow = yaml.safe_load((ROOT / ".github/workflows/desktop-shell.yml").read_text())
    steps = workflow["jobs"]["shell"]["steps"]
    upload = next(step for step in steps if step.get("name") == "Upload Windows Cua Driver security evidence")
    assert upload["with"]["if-no-files-found"] == "error"
    paths = upload["with"]["path"].splitlines()
    assert len(paths) == 3
    assert [path.rsplit("*/", 1)[-1] for path in paths] == [
        "provenance.json",
        "junit-*.xml",
        "logs/**",
    ]
