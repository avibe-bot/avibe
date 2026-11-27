"""ios-shell reruns a failed XCTest once only when the simulator stalled under it.

Contract: the XCTest step passes after a failure only when every failed test's
own log lines show a WebKit or simulator stall and the rerun of exactly those
tests passes. A crash, an incomplete run, a failure without a stall, or too
many failures fails the step without a rerun. The step's own shell runs here
against a stand-in ``xcodebuild``.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

STARTED = "Test Case '-[AvibeTests.{cls} {method}]' started."
FAILED = "Test Case '-[AvibeTests.{cls} {method}]' failed (131.147 seconds)."
PASSED = "Test Case '-[AvibeTests.{cls} {method}]' passed (0.010 seconds)."
APP = "2026-11-26 11:17:12.391778+0000 Avibe[26575:76503] [Process] "
SLOW_LAUNCH = APP + "WebContent process (0x1151300c0) took 30.339143 seconds to launch"
HEALTHY_LAUNCH = APP + "WebContent process (0x1151300c0) took 4.306182 seconds to launch"
UNRESPONSIVE = APP + "0x1151300c0 - [PID=41514] WebProcessProxy::didBecomeUnresponsive:"
GPU_UNRESPONSIVE = (
    APP + "0x114168200 - GPUProcessProxy::didBecomeUnresponsive: GPUProcess with PID 46747 "
    "became unresponsive, terminating it"
)
SLOW_BOOTSTRAP = "[iOS fixture] stage=bootstrap settled, elapsed=126.071802084 seconds, route=x"
HEALTHY_BOOTSTRAP = "[iOS fixture] stage=bootstrap settled, elapsed=7.781541666 seconds, route=x"
ERROR = "/Users/runner/work/avibe/avibe/ios/AvibeTests/T.swift:1: error: -[AvibeTests.{cls} {method}] : failed"
RESTARTED = "Restarting after unexpected exit, crash, or test timeout; summary will include totals"
ENDED = ["Test Suite 'All tests' failed at 2026-11-26 11:52:03.198.", "** TEST FAILED **"]


def failure(method: str, *lines: str, cls: str = "DirectPathAppTests") -> list[str]:
    names = {"cls": cls, "method": method}
    return [
        STARTED.format(**names),
        *lines,
        ERROR.format(**names),
        FAILED.format(**names),
    ]


def passing(method: str, *lines: str) -> list[str]:
    names = {"cls": "DirectPathAppTests", "method": method}
    return [STARTED.format(**names), *lines, PASSED.format(**names)]


RERUN = {
    "slow launch": [*failure("testA", SLOW_LAUNCH), *ENDED],
    "WebContent unresponsive": [*failure("testA", HEALTHY_LAUNCH, UNRESPONSIVE), *ENDED],
    "GPU unresponsive": [*failure("testA", GPU_UNRESPONSIVE), *ENDED],
    "stalled bootstrap": [*failure("testA", SLOW_BOOTSTRAP), *ENDED],
    "two stalled tests": [
        *failure("testA", SLOW_LAUNCH),
        *passing("testB"),
        *failure("testC", UNRESPONSIVE, cls="NavigationPolicyTests"),
        *ENDED,
    ],
}

STANDS = {
    # The stall belongs to a test that passed, not to the one that failed.
    "stall in another test": [*passing("testA", SLOW_LAUNCH), *failure("testB"), *ENDED],
    "healthy launch and bootstrap": [
        *failure("testA", HEALTHY_LAUNCH, HEALTHY_BOOTSTRAP),
        *ENDED,
    ],
    "one of two failures without a stall": [
        *failure("testA", SLOW_LAUNCH),
        *failure("testB"),
        *ENDED,
    ],
    "crash": [*failure("testA", SLOW_LAUNCH), RESTARTED, *ENDED],
    "incomplete run": failure("testA", SLOW_LAUNCH),
    "no failed test": ["error: build failed", *ENDED],
    "four stalled tests": [
        *[line for name in "ABCD" for line in failure(f"test{name}", SLOW_LAUNCH)],
        *ENDED,
    ],
}


def run_step(
    tmp_path: Path, log: list[str], rerun_exit: int = 0
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the workflow's XCTest step with a stand-in xcodebuild."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ios-shell.yml").read_text(encoding="utf-8"))
    step, = [
        step for step in workflow["jobs"]["simulator"]["steps"]
        if step.get("name") == "Run XCTest on iPhone Simulator"
    ]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    first_log = tmp_path / "first.log"
    first_log.write_text("\n".join(log) + "\n", encoding="utf-8")
    xcodebuild = bin_dir / "xcodebuild"
    # The first call is the full run, which prints the log and fails; any
    # later call is a rerun and ends with ``rerun_exit``.
    xcodebuild.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{calls}"\n'
        f'if [ "$(wc -l < "{calls}")" -eq 1 ]; then cat "{first_log}"; exit 65; fi\n'
        f"exit {rerun_exit}\n",
        encoding="utf-8",
    )
    xcodebuild.chmod(xcodebuild.stat().st_mode | stat.S_IEXEC)
    runner_temp = tmp_path / "temp"
    runner_temp.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
        "SIMULATOR_ID": "SIM",
    }
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", step["run"]],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    return result, calls.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("case", RERUN)
def test_a_failure_the_simulator_stalled_runs_once_more_alone(tmp_path: Path, case: str) -> None:
    result, calls = run_step(tmp_path, RERUN[case])

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert calls[0].endswith(" test")
    rerun = calls[1].split()
    assert rerun[-1] == "test-without-building"
    failed = [line for line in RERUN[case] if "' failed (" in line]
    expected = {
        "-only-testing:AvibeTests/" + line.split("[AvibeTests.", 1)[1].split("]", 1)[0].replace(" ", "/")
        for line in failed
    }
    assert {arg for arg in rerun if arg.startswith("-only-testing:")} == expected
    assert '"$RUNNER_TEMP/AvibeTestDerivedData"' not in calls[1]
    assert f"{tmp_path}/temp/AvibeTestDerivedData" in calls[1]
    assert "::warning title=Simulator stall::" in result.stdout
    # The lines that matched are printed, so the rerun shows its evidence.
    stalls = [SLOW_LAUNCH, UNRESPONSIVE, GPU_UNRESPONSIVE, SLOW_BOOTSTRAP]
    printed = result.stdout.split("Matched lines of ", 1)[1]
    for line in RERUN[case]:
        assert (f"  {line}\n" in printed) == (line in stalls)


@pytest.mark.parametrize("case", STANDS)
def test_any_other_failure_stands_without_a_rerun(tmp_path: Path, case: str) -> None:
    result, calls = run_step(tmp_path, STANDS[case])

    assert result.returncode != 0
    assert len(calls) == 1
    assert "Not rerun:" in result.stderr


def test_a_rerun_that_fails_again_fails_the_step(tmp_path: Path) -> None:
    result, calls = run_step(tmp_path, RERUN["slow launch"], rerun_exit=65)

    assert result.returncode != 0
    assert len(calls) == 2
