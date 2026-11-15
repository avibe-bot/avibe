"""Contracts for the draft-only deterministic model fixture, not product smoke.

The credible regression is dispatching a nonexistent Windows tool, dispatching
unrequested text, or executing the same fixture command again after a tool
result. Existing provider-adapter tests cannot cover this HTTP producer.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Thread
from urllib.request import Request, urlopen

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "signed_cu_model_stub", ROOT / "desktop/scripts/signed-cu-acceptance/model_stub.py"
)
assert SPEC and SPEC.loader
stub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stub)


def test_draft_workflow_cannot_dispatch_or_receive_release_write_authority():
    # Observable workflow contract: an accidental broad trigger, write token,
    # self-hosted runner, or long retention would violate this draft's mandate.
    workflow = yaml.load(
        (ROOT / ".github/workflows/windows-signed-cu-acceptance.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"pull_request"}
    # Test-only fixes must still produce the exact-head native check watched by
    # this draft; path filtering must not leave that gate permanently missing.
    paths = workflow["on"]["pull_request"]["paths"]
    assert "tests/test_windows_signed_acceptance.py" in paths
    assert "tests/windows_signed_acceptance_*" in paths
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    job = workflow["jobs"]["signed-installer"]
    assert job["runs-on"] == "windows-latest"
    assert job["timeout-minutes"] == "45"
    assert "github.event.pull_request.number == 3785" in job["if"]
    assert "github.event.pull_request.draft == true" in job["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job["if"]
    uploads = [step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    assert uploads[0]["with"]["retention-days"] == "7"
    assert uploads[0]["with"]["path"].splitlines() == [
        "${{ runner.temp }}/signed-cu-evidence/verdict.json",
        "${{ runner.temp }}/signed-cu-evidence/verdict.md",
    ]


@pytest.mark.parametrize(("fixture", "script", "receipt"), [
    ("release", "release", "release_proof_fixture_cases_passed"),
    ("observer", "update-observer", "update_observer_fixture_cases_passed"),
    ("evidence", "evidence", "evidence_fixture_cases_passed"),
])
def test_powershell_acceptance_oracles_fail_closed(tmp_path, fixture, script, receipt):
    # Real helpers, hermetic boundary substitutes: reject bad release proof,
    # premature failure teardown, and stale/reused CDP generations. Existing
    # provider/DOM tests cannot exercise these process-observer boundaries.
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is required for the hermetic acceptance oracles")
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "SystemRoot", "WINDIR"}}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMPDIR", "TEMP", "TMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        env[key] = str(tmp_path)
    outcome = subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
         str(ROOT / f"tests/windows_signed_acceptance_{fixture}.ps1"),
         "-Source", str(ROOT / f"desktop/scripts/signed-cu-acceptance/{script}.ps1"),
         "-Work", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert outcome.returncode == 0, outcome.stdout + outcome.stderr
    assert receipt in outcome.stdout


def test_update_panel_oracle_rejects_stale_check_and_changed_offer(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the hermetic CDP-oracle VM")
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "SystemRoot", "WINDIR"}}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMPDIR", "TEMP", "TMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        env[key] = str(tmp_path)
    outcome = subprocess.run(
        [node, str(ROOT / "tests/windows_signed_acceptance_oracle.mjs"),
         "--source", str(ROOT / "desktop/scripts/signed-cu-acceptance/cdp.mjs")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20,
    )
    assert outcome.returncode == 0, outcome.stdout + outcome.stderr


def test_enabled_generated_pipeline_preserves_faults_and_final_verdict(tmp_path):
    # Execute the same generator, tool command, wrapper, JS, receipt reader,
    # serializer and final gate. Only OS/CLI/desktop boundaries are inert. This
    # catches bypassed readiness/focus and pre-diagnostic exceptions which the
    # prior source-text assertions could never detect.
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is required for the generated pipeline oracle")
    work = tmp_path / "acceptance work's Unicode-测试"
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "SystemRoot", "WINDIR"}}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMPDIR", "TEMP", "TMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        env[key] = str(tmp_path)
    outcome = subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
         str(ROOT / "tests/windows_signed_acceptance_enabled.ps1"),
         "-SourceRoot", str(ROOT / "desktop/scripts/signed-cu-acceptance"),
         "-Work", str(work), "-Evidence", str(tmp_path / "evidence")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600,
    )
    assert outcome.returncode == 0, outcome.stdout + outcome.stderr
    assert "enabled_input_dry_cases_passed" in outcome.stdout
    verdict = json.loads((tmp_path / "evidence/verdict.json").read_text(encoding="utf-8-sig"))
    assert verdict["product_acceptance"] is False
    assert len(verdict["cases"]) >= 22
    assert all(case["passed"] for case in verdict["cases"])


def prepare_context(work: Path, marker="launch-off"):
    work.mkdir(parents=True, exist_ok=True)
    (work / f"{marker}-context.json").write_text(json.dumps({"operation_id": "a" * 32}))


def request(marker="launch-off", tool="powershell"):
    return {
        "messages": [{"role": "user", "content": f"AVIBE_CU_CASE:{marker}"}],
        "tools": [{"type": "function", "function": {"name": tool}}],
    }


def test_windows_tool_and_real_cli_command_are_bound_to_fixture_marker(tmp_path):
    work = tmp_path / 'acceptance work'
    prepare_context(work)
    reply, finish = stub.scripted_reply(request(), python="C:/installed python.exe", work=work)
    call = reply["tool_calls"][0]
    assert finish == "tool_calls"
    assert call["function"]["name"] == "powershell"
    args = json.loads(call["function"]["arguments"])
    assert f'-File "{work / "launch-off.ps1"}"' in args["command"]
    assert '-Python "C:/installed python.exe"' in args["command"]
    assert "-File '" not in args["command"]
    assert "launch-off.js" in args["command"]
    assert "launch-off-type.js" in args["command"]
    assert "WindowsPowerShell" in args["command"]
    assert "AVIBE_SESSION_ID" not in args["command"]
    assert args["watch"] is False
    assert args["timeout"] == 150


def test_launch_wrapper_uses_native_windows_argv_handoff(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX ProcessStartInfo argv oracle; hosted Windows uses a native executable fixture")
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is required for the argv handoff oracle")
    work = tmp_path / "acceptance work"
    prepare_context(work)
    fake_root = tmp_path / "system root"
    fake = fake_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$FAKE_ARGV\"\n", encoding="utf-8")
    fake.chmod(0o755)
    argv_file = tmp_path / "argv.txt"
    reply, _ = stub.scripted_reply(
        request(), python=str(tmp_path / "python path.exe"), work=work
    )
    command = json.loads(reply["tool_calls"][0]["function"]["arguments"])["command"]
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "SystemRoot", "WINDIR"}}
    for key in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        env[key] = str(tmp_path)
    env.update({"SystemRoot": str(fake_root), "FAKE_ARGV": str(argv_file)})
    outcome = subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert outcome.returncode == 0, outcome.stdout + outcome.stderr
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("-File") + 1] == str(work / "launch-off.ps1")
    assert argv[argv.index("-Python") + 1] == str(tmp_path / "python path.exe")
    assert argv[argv.index("-LaunchScript") + 1] == str(work / "launch-off.js")
    assert argv[argv.index("-TypeScript") + 1] == str(work / "launch-off-type.js")


def test_windows_probe_still_uses_direct_shipped_js_command(tmp_path):
    reply, finish = stub.scripted_reply(request(marker="probe-context"), python="C:/installed/python.exe", work=tmp_path)
    command = json.loads(reply["tool_calls"][0]["function"]["arguments"])["command"]
    assert finish == "tool_calls"
    assert "-I -B -m vibe computer js" in command
    assert "probe-context.js" in command


@pytest.mark.parametrize("payload", [request(tool="bash"), request(marker="delete-everything"), {"messages": [], "tools": []}])
def test_unrequested_or_non_windows_tool_call_is_refused(payload, tmp_path):
    with pytest.raises(ValueError):
        stub.scripted_reply(payload, python="python.exe", work=tmp_path)


def test_actual_tool_result_finishes_without_reexecuting_or_claiming_acceptance(tmp_path):
    payload = request()
    payload["messages"].append({"role": "tool", "tool_call_id": "call_cu_launch_off", "content": "failed"})
    reply, finish = stub.scripted_reply(payload, python="python.exe", work=tmp_path)
    assert finish == "stop"
    assert "tool_calls" not in reply
    assert "external observer" in reply["content"]
    assert "PASS" not in reply["content"]


def test_actual_loopback_wire_has_tool_call_finish_and_done(tmp_path):
    prepare_context(tmp_path)
    server = stub.ThreadingHTTPServer(("127.0.0.1", 0), stub.handler("python.exe", tmp_path))
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        with urlopen(Request(url, data=json.dumps(request()).encode(), headers={"Content-Type": "application/json"}), timeout=5) as response:
            assert response.headers["Content-Type"] == "text/event-stream"
            frames = response.read().decode().split("\n\n")
        events = [json.loads(frame.removeprefix("data: ")) for frame in frames if frame.startswith("data: {")]
        assert events[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "powershell"
        assert events[1]["choices"][0]["finish_reason"] == "tool_calls"
        assert "data: [DONE]" in frames
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
