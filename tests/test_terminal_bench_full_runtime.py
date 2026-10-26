"""Boundary regressions for the source-only full-product benchmark supervisor.

Observable contracts: fresh disjoint state; inherited caller credentials cannot
route into the live service; real CLI Run dispatch writes task files while all
runtime state stays out; deadline cleanup is owned and bounded. Existing service
acceptance covers real CPA; these tests substitute ONLY that external engine.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import psutil
import pytest

from scripts.terminal_bench.profile import Profile, validate_layout
from scripts.terminal_bench.runner import FullRuntimeTrial, OwnedProcesses, TrialError

ROOT = Path(__file__).resolve().parents[1]
# Fresh SQLite migrations reached Controller construction only after 30s in
# cold HD1 runs. Startup gets its own 120s allowance; this does not relax the
# 30s Agent deadline or the deliberately short 2s cancellation scenario.
PROFILE = Profile(model="fixture-model", base_url="http://127.0.0.1:9/v1", setup_seconds=120,
                  wall_seconds=30, cleanup_seconds=5, poll_seconds=0.1, offline=True)


def test_disjoint_fresh_layout_and_environment(tmp_path, monkeypatch):
    task = tmp_path / "任务"
    task.mkdir()
    monkeypatch.setenv("AVIBE_SESSION_ID", "real-user-session")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "real-user-key")
    before = dict(os.environ)
    trial = FullRuntimeTrial(task, tmp_path / "private", tmp_path / "export", PROFILE)
    assert dict(os.environ) == before
    assert "AVIBE_SESSION_ID" not in trial.env
    assert "ANTHROPIC_AUTH_TOKEN" not in trial.env
    assert "DEEPSEEK_API_KEY" not in trial.env
    assert Path(trial.env["AVIBE_HOME"]).is_relative_to(trial.state)
    assert not list(task.iterdir())
    with pytest.raises(FileExistsError):
        FullRuntimeTrial(task, tmp_path / "private", tmp_path / "other-export", PROFILE)
    for state, output in [(task / "state", tmp_path / "out"), (tmp_path, tmp_path / "out"),
                          (tmp_path / "private", tmp_path / "private" / "export")]:
        with pytest.raises(ValueError):
            validate_layout(task, state, output)
    alias = tmp_path / "alias"
    alias.symlink_to(task, target_is_directory=True)
    with pytest.raises(ValueError):
        validate_layout(task, alias, tmp_path / "out")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "custom-user-state"))
    with pytest.raises(ValueError):
        validate_layout(task, tmp_path / "custom-user-state" / "bench", tmp_path / "out")


def test_sandbox_environment_retains_task_home_and_toolchains(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    task_env = {"HOME": "/root", "JAVA_HOME": "/opt/java", "RUSTUP_HOME": "/opt/rust",
                "VIRTUAL_ENV": "/opt/task-venv", "PYTHONPATH": "/task-modules",
                "AVIBE_SESSION_ID": "host-session", "DEEPSEEK_API_KEY": "private",
                "VIBE_INTERNAL_DISPATCH_SOCKET": "/live/socket", "TASK_DB_PASSWORD": "task-local-value"}
    trial = FullRuntimeTrial(task, tmp_path / "state", tmp_path / "out", PROFILE, task_environment=task_env)
    for name in ["HOME", "JAVA_HOME", "RUSTUP_HOME", "VIRTUAL_ENV", "PYTHONPATH", "TASK_DB_PASSWORD"]:
        assert trial.env[name] == task_env[name]
    for name in ["AVIBE_SESSION_ID", "DEEPSEEK_API_KEY", "VIBE_INTERNAL_DISPATCH_SOCKET"]:
        assert name not in trial.env
    assert task_env["AVIBE_SESSION_ID"] == "host-session"  # caller mapping was not mutated


@pytest.mark.reap_processes
@pytest.mark.allow_foreign_signals("Full-runtime supervisor reaps its subprocess's recorded descendants by identity")
@pytest.mark.parametrize("scenario", ["success", "watch", "deadline", "missing_credential", "empty-response", "thinking-length"])
def test_real_service_cli_hub_vibey_writes_and_isolates_state(tmp_path, monkeypatch, scenario):
    task = tmp_path / "任务"
    task.mkdir()
    # This must not shadow product imports, nor be deleted/rewritten.
    (task / "config.py").write_text("task_owned = True\n")
    profile = replace(PROFILE, wall_seconds=2) if scenario == "deadline" else PROFILE
    trial = FullRuntimeTrial(task, tmp_path / "state", tmp_path / "export", profile, source_root=ROOT)
    trial.env["JAVA_HOME"] = "/fixture/java-toolchain"
    if scenario == "watch":
        trial.env["TB_TEST_SCENARIO"] = "watch"
        # Source-only test interpreter: the measured installed wheel has this
        # executable already. The shim lives outside the task and prompt.
        shim = trial.state / "bin" / "vibe"
        shim.parent.mkdir()
        shim.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ROOT)!r})\nfrom vibe.cli import main\nmain()\n")
        shim.chmod(0o755)
        trial.env["PATH"] = str(shim.parent) + os.pathsep + trial.env["PATH"]
    if scenario == "deadline":
        trial.env["TB_TEST_SCENARIO"] = "slow-tool"
    if scenario in {"empty-response", "thinking-length"}:
        trial.env["TB_TEST_SCENARIO"] = scenario
    original_command = trial._command

    def command(operation, *args):
        if operation == "serve":
            return [sys.executable, "-I", str(ROOT / "tests/fixtures/full_runtime_service.py")]
        return original_command(operation, *args)

    monkeypatch.setattr(trial, "_command", command)
    before = dict(os.environ)
    with trial:
        assert trial._result["controller_health"]["service"] == "vibe-remote-internal"
        agents = trial._call("cli", "agent", "list")
        assert any(agent["backend"] == "vibey" for agent in agents["agents"])
        key = None if scenario == "missing_credential" else "fixture-private-key-not-real"
        trial.execute("Complete the synthetic tool-write exercise.", key=key)
        if scenario == "success":
            assert trial._result["outcome"] == "succeeded", trial._result
            assert trial._result["run_id"] and trial._result["session_id"]
            assert (task / "笔记.md").read_text(encoding="utf-8") == "可查询的任务笔记"
            with sqlite3.connect(task / "task.sqlite") as db:
                assert db.execute("SELECT value FROM evidence").fetchall() == [("task-data",)]
            env = json.loads((task / "task-env.json").read_text())
            assert env["cwd"] == str(task)
            assert env["session"] == trial._result["session_id"]  # real Harness identity
            assert env["credential_present"] is False
            assert env["java_home"] == "/fixture/java-toolchain"
            assert Path(env["home"]) == trial.home
        elif scenario == "watch":
            assert trial._result["outcome"] == "succeeded", trial._result
            assert trial._result["harness_quiescent"] is True
            assert (task / "watch-note.txt").read_text() == "continued"
        elif scenario == "deadline":
            assert trial._result["outcome"] == "time_limit", trial._result
            assert trial._result["run_id"]
            assert (task / "still-writing").exists(), "the real tool must run before the deadline"
        elif scenario in {"empty-response", "thinking-length"}:
            assert trial._result["outcome"] == "failed", trial._result
            assert trial._result["harness_quiescent"] is True
            expected_error = "output limit" if scenario == "thinking-length" else "empty reply"
            assert expected_error in trial._result["final_text"]
        else:
            assert trial._result["outcome"] == "blocked"
            assert trial._result["blocker"] == "model_source_credential_required"
            assert trial._result["run_id"] is None
        assert (trial.avibe_home / "state" / "vibe.sqlite").exists()
        assert not (trial.avibe_home / "runtime" / "vibe-ui.pid").exists()
        assert not (trial.avibe_home / "runtime" / "remote-access-cloudflared.pid").exists()
    assert dict(os.environ) == before
    expected = {"config.py", "笔记.md", "task.sqlite", "task-env.json"} if scenario in {"success", "empty-response", "thinking-length"} else {"config.py"}
    if scenario == "watch":
        expected.add("watch-note.txt")
    if scenario == "deadline":
        expected.add("still-writing")
        final = (task / "still-writing").read_bytes()
        time.sleep(.15)
        assert (task / "still-writing").read_bytes() == final, "no late write may race the grader"
    assert {p.name for p in task.iterdir()} == expected
    report_text = (trial.output / "execution.json").read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert "fixture-private-key-not-real" not in report_text
    if scenario == "success":
        assert "[REDACTED]" in report["final_text"]
    if scenario in {"success", "empty-response", "thinking-length"}:
        diagnostic = report["diagnostics"]
        assert diagnostic["status"] == "available", diagnostic
        last = diagnostic["responses"][0]
        assert last["stop_reason"] == ("length" if scenario == "thinking-length" else "stop")
        assert last["thinking_blocks"] == int(scenario == "thinking-length")
        assert last["text_blocks"] == int(scenario == "success")
        assert last["tool_calls"] == 0
        assert "private-reasoning" not in json.dumps(diagnostic)
    assert report["cleanup"]["owned_processes_stopped"] is True
    assert report["verifier_reward"] is None
    assert report["accounting_complete"] is False
    assert {p.name for p in trial.output.iterdir()} == {"execution.json"}
    assert not trial._processes._live()


@pytest.mark.reap_processes
@pytest.mark.allow_foreign_signals("Supervisor owns the spawned process and its captured setsid child")
def test_owned_cleanup_reaps_detached_child_and_preserves_bystander(tmp_path):
    marker = "owned-cleanup-fixture"
    pidfile = tmp_path / "child-pid"
    child = "import time; time.sleep(60)"
    program = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}], start_new_session=True); "
        "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    )
    env = {"PATH": os.defpath, "AVIBE_BENCHMARK_OWNER": marker}
    process = subprocess.Popen([sys.executable, "-c", program, str(pidfile)], env=env, start_new_session=True)
    bystander = subprocess.Popen([sys.executable, "-c", child], start_new_session=True)
    owner = OwnedProcesses(process, marker)
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert pidfile.exists()
        child_pid = int(pidfile.read_text())
        # A clock shift must not make an owned, still-live process disappear
        # from verification. A reused unrelated pid must not become authority.
        with owner._lock:
            owner.identities[process.pid] -= .25
            owner.identities[bystander.pid] = psutil.Process(bystander.pid).create_time() - 1.0
        assert process.pid in {p.pid for p in owner._live()}
        assert bystander.pid not in {p.pid for p in owner._live()}
        result = owner.close(.1)
        assert result["owned_processes_stopped"] is True
        assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        assert bystander.poll() is None
    finally:
        if process.poll() is None:
            owner.close(.1)
        bystander.kill()
        bystander.wait(timeout=5)


@pytest.mark.parametrize("stage, failure, code", [
    ("startup", "timeout", "initialize_helper_timeout"),
    ("poll", "timeout", "cli_helper_timeout"),
    ("poll", "exit", "cli_helper_failed_2"),
    ("poll", "signal", "cli_helper_failed_-9"),
    # Run-show validates the durable Run payload, not just generic helper JSON.
    ("poll", "json", "cli_helper_invalid_run_payload"),
    ("poll", "quiescence", "harness_quiescence_unconfirmed"),
    ("poll", "unsafe_trial_error", "unclassified_trial_error"),
    ("poll", "upstream_exception", None),
])
def test_failure_report_preserves_only_wrapper_codes(tmp_path, monkeypatch, capsys, stage, failure, code):
    """The CLI/report boundary lost safe codes; raw exception bodies can leak keys.

    The existing subprocess happy-path test cannot expose that loss. Inject the
    helper failure, but use real _call conversion, context exit, report and main.
    """
    from scripts.terminal_bench import runner

    private = "private-upstream-error-not-for-export"
    task = tmp_path / "task"
    task.mkdir()
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(json.dumps(PROFILE.payload()))
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("fixture")
    monkeypatch.setenv(PROFILE.credential_env, "fixture-key")
    if stage == "poll":
        monkeypatch.setattr(FullRuntimeTrial, "start", lambda self: self._result.update(outcome="ready"))

    def helper(argv, **kwargs):
        is_poll = "runs" in argv and "show" in argv
        if stage == "startup" or is_poll:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=private, stderr=private)
            if failure in {"exit", "signal"}:
                return subprocess.CompletedProcess(argv, 2 if failure == "exit" else -9, private, private)
            if failure == "json":
                return subprocess.CompletedProcess(argv, 0, private, "")
            if failure == "unsafe_trial_error":
                raise TrialError(private)
            if failure == "upstream_exception":
                raise ValueError(private)
            body = {"run": {"status": "succeeded", "result_text": "fixture"}}
        elif "agent" in argv and "run" in argv:
            body = {"run": {"id": "fixture-run", "session_id": "fixture-session"}}
        elif "harness" in argv:
            body = {"controller": {"available": False}}
        else:
            body = {}
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    monkeypatch.setattr(subprocess, "run", helper)
    assert runner.main([
        "--workdir", str(task), "--state", str(tmp_path / "private-state"),
        "--output", str(tmp_path / "output"), "--profile", str(profile_file),
        "--instruction", str(instruction),
    ]) == 2
    captured = capsys.readouterr()
    report_text = (tmp_path / "output" / "execution.json").read_text()
    assert private not in report_text + captured.out + captured.err
    for payload in (json.loads(report_text), json.loads(captured.out), json.loads(captured.err)):
        assert payload["outcome"] == "infrastructure_error"
        assert payload["error_type"] == ("ValueError" if failure == "upstream_exception" else "TrialError")
        if code is None:
            assert "error_code" not in payload
        else:
            assert payload["error_code"] == code
