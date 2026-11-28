"""Host-side checks for the hosted native runner, not Windows execution evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
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


def driver_job():
    workflow = yaml.safe_load((ROOT / ".github/workflows/windows-driver.yml").read_text())
    return workflow["jobs"]["verify"]


def test_production_capture_is_a_required_parallel_windows_job():
    jobs = yaml.safe_load((ROOT / ".github/workflows/desktop-shell.yml").read_text())["jobs"]
    (caller,) = (job for job in jobs.values() if job.get("uses") == "./.github/workflows/windows-driver.yml")
    job = driver_job()
    for required in (caller, job):
        assert not {"if", "needs", "continue-on-error"}.intersection(required)
    assert job["runs-on"] == "windows-latest"
    steps = job["steps"]
    (checkout,) = (s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert all("continue-on-error" not in step and ("if" not in step or step["if"] == "always()") for step in steps)
    (source,) = (step for step in steps if "test-windows-driver-source.ps1" in step.get("run", ""))
    assert "-CaptureProductionDriver" in source["run"]
    upload = next(step for step in steps if step.get("with", {}).get("name", "").startswith("windows-cua-driver-production-"))
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["path"].strip().endswith("production/**")


@pytest.mark.parametrize("path", [
    ".github/workflows/windows-driver.yml",
    ".github/workflows/desktop-shell.yml",
    "tests/test_windows_driver_source_harness.py",
])
def test_driver_gate_changes_require_native_execution(path):
    from scripts.ci_docs_only import scope_of
    from scripts.ci_fast_pr import selected

    _, jobs = selected(scope_of([("M", "100644", "100644", path)], True))
    assert "desktop-shell" in jobs


@pytest.mark.parametrize("fault", [None, "checkout", "provenance", "binary", "receipts"])
def test_receipt_index_binds_candidate_binary_and_native_results(tmp_path, fault):
    (step,) = (s for s in driver_job()["steps"] if s.get("shell") == "python")
    root = tmp_path / "avibe-windows-driver-security-fixture"
    (root / "production").mkdir(parents=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    binary_digest = hashlib.sha256(b"binary").hexdigest()
    (root / "production/cua-driver.exe").write_bytes(b"binary" if fault != "binary" else b"different")
    (root / "provenance.json").write_text(json.dumps({
        "avibe_head": head if fault != "provenance" else "a" * 40,
        "production_binary_sha256": binary_digest,
    }))
    receipts = {"junit-no-skip-count-guard.xml": b"guard", "junit-case.xml": b"case"}
    for name, content in receipts.items():
        if fault != "receipts" or name != "junit-case.xml":
            (root / name).write_bytes(content)
    result = subprocess.run(
        [sys.executable, "-c", step["run"]], cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_SHA": head if fault != "checkout" else "b" * 40},
    )
    index = root / "candidate.json"
    if fault:
        expected = {
            "checkout": "candidate checkout", "provenance": "candidate checkout",
            "binary": "Production binary", "receipts": "Missing native test receipts",
        }
        assert result.returncode != 0 and expected[fault] in result.stderr
        assert not index.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(index.read_text()) == {
            "candidate_sha": head, "production_binary_sha256": binary_digest,
            "junit_sha256": {k: hashlib.sha256(v).hexdigest() for k, v in receipts.items()},
        }


def test_windows_manifest_records_ordered_final_patch():
    manifest = json.loads((ROOT / "desktop/cua-driver/sources.json").read_text())
    windows = manifest["driver"]["windows"]
    assert windows["patch_order"] == [
        manifest["driver"]["patch"]["file"],
        windows["patch"]["file"],
    ]
    assert windows["patches"] == [
        {"file": patch["file"], "sha256": patch["sha256"]}
        for patch in [manifest["driver"]["patch"], windows["patch"]]
    ]
    for patch in windows["patches"]:
        content = (ROOT / "desktop/cua-driver" / patch["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == patch["sha256"]
    assert windows["patch"]["sha256"] == "5f277c1bfc5e0e3d07710e903cf15a68ef5ee3d07f87de1fbe0cf64f364a10a0"


def test_native_evidence_upload_is_bounded():
    # Environment/profile, exact selectors and XML validation are exercised by
    # the native job. Do not pretend that finding their implementation spelling
    # in a script proves they execute. This check owns only the published
    # artifact-name/path contract in the parsed workflow configuration.
    steps = driver_job()["steps"]
    upload = next(
        step for step in steps if step.get("with", {}).get("name", "").startswith("windows-cua-driver-security-")
    )
    assert upload["with"]["if-no-files-found"] == "error"
    paths = upload["with"]["path"].splitlines()
    assert [path.rsplit("*/", 1)[-1] for path in paths] == [
        "candidate.json",
        "provenance.json",
        "junit-*.xml",
        "logs/**",
    ]
