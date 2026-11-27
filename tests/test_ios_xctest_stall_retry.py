"""ios-shell reruns a failed XCTest once only when the simulator stalled under it.

Contract: the XCTest step passes after a failure only when every failed test's
own log lines show a WebKit or simulator stall and the rerun of exactly those
tests passes. A crash, an incomplete run, a failure without a stall, or too
many failures fails the step without a rerun. The step's own shell runs here
against a stand-in ``xcodebuild``.
"""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ios" / "scripts"))
from xctest_stall_retry import weekly_counts  # noqa: E402

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
ERROR = "/Users/runner/work/avibe/avibe/ios/AvibeTests/T.swift:1: error: -[AvibeTests.{cls} {method}] : {message}"
WAIT = (
    "Asynchronous wait failed: Exceeded timeout of 60 seconds, with unfulfilled "
    'expectations: "/api/two-factor/verify in flight".'
)
EVENTUALLY = "failed - The condition did not become true within 140.0 seconds."
FIXTURE = (
    "failed - The signed-in fixture settled on another state: stage=bootstrap settled, "
    "elapsed=120.48599054099999 seconds, route=onboarding"
)
QUICK_FIXTURE = (
    "failed - The signed-in fixture settled on another state: stage=bootstrap settled, "
    "elapsed=2.1 seconds, route=onboarding"
)
ASSERTION = "XCTAssertTrue failed"
RESTARTED = "Restarting after unexpected exit, crash, or test timeout; summary will include totals"
ENDED = ["Test Suite 'All tests' failed at 2026-11-26 11:52:03.198.", "** TEST FAILED **"]


def failure(
    method: str, *lines: str, cls: str = "DirectPathAppTests", message: str = WAIT,
    after: tuple[str, ...] = (),
) -> list[str]:
    """A failed test: its lines, its first failure, then ``after``."""
    names = {"cls": cls, "method": method}
    return [
        STARTED.format(**names),
        *lines,
        ERROR.format(**names, message=message),
        *after,
        FAILED.format(**names),
    ]


def passing(method: str, *lines: str) -> list[str]:
    names = {"cls": "DirectPathAppTests", "method": method}
    return [STARTED.format(**names), *lines, PASSED.format(**names)]


RERUN = {
    "slow launch": [*failure("testA", SLOW_LAUNCH), *ENDED],
    "WebContent unresponsive": [
        *failure("testA", HEALTHY_LAUNCH, UNRESPONSIVE, message=EVENTUALLY), *ENDED,
    ],
    "GPU unresponsive": [*failure("testA", GPU_UNRESPONSIVE), *ENDED],
    "stalled bootstrap": [*failure("testA", SLOW_BOOTSTRAP, message=FIXTURE), *ENDED],
    # The host's log split a passing test's own passed line.
    "a passed line split by the host's log": [
        STARTED.format(cls="DirectPathAppTests", method="testZ"),
        "Test Case '-[AvibeTests.DirectPathAppTests tes2026-11-26 20:11:28.258037+0000 Avibe[1:2] x",
        "tZ]' passed (0.194 seconds).",
        *failure("testA", SLOW_LAUNCH),
        *ENDED,
    ],
    "two stalled tests": [
        *failure("testA", SLOW_LAUNCH),
        *passing("testB"),
        *failure("testC", UNRESPONSIVE, cls="NavigationPolicyTests", message=EVENTUALLY),
        *ENDED,
    ],
}

# Each case with the reason it stands for, so a case cannot pass by being
# turned down for some other reason.
STANDS = {
    # The stall belongs to a test that passed, not to the one that failed.
    "stall in another test": (
        [*passing("testA", SLOW_LAUNCH), *failure("testB"), *ENDED],
        "no simulator stall before the first failure of DirectPathAppTests.testB",
    ),
    "healthy launch and bootstrap": (
        [*failure("testA", HEALTHY_LAUNCH, HEALTHY_BOOTSTRAP), *ENDED],
        "no simulator stall before the first failure of DirectPathAppTests.testA",
    ),
    # The stall came after the test had already failed (master, 11-22).
    "stall after the first failure": (
        [*failure("testA", message=WAIT, after=(UNRESPONSIVE,)), *ENDED],
        "no simulator stall before the first failure of DirectPathAppTests.testA",
    ),
    # A stall does not excuse an assertion.
    "stall and a plain assertion": (
        [*failure("testA", SLOW_LAUNCH, UNRESPONSIVE, message=ASSERTION), *ENDED],
        "the first failure is not a wait that ran out in DirectPathAppTests.testA",
    ),
    # The bootstrap settled quickly on the wrong state: a state failure.
    "stall and a quick wrong bootstrap": (
        [*failure("testA", SLOW_LAUNCH, message=QUICK_FIXTURE), *ENDED],
        "the first failure is not a wait that ran out in DirectPathAppTests.testA",
    ),
    "one of two failures without a stall": (
        [*failure("testA", SLOW_LAUNCH), *failure("testB"), *ENDED],
        "no simulator stall before the first failure of DirectPathAppTests.testB",
    ),
    "crash": (
        [*failure("testA", SLOW_LAUNCH), RESTARTED, *ENDED],
        "crashed or timed out and was restarted",
    ),
    "incomplete run": (failure("testA", SLOW_LAUNCH), "the run did not complete"),
    "no failed test": (["error: build failed", *ENDED], "no test failed"),
    "four stalled tests": (
        [*[line for name in "ABCD" for line in failure(f"test{name}", SLOW_LAUNCH)], *ENDED],
        "4 tests failed",
    ),
    "failed without its started line": (
        [SLOW_LAUNCH, *failure("testA", SLOW_LAUNCH)[2:], *ENDED],
        "DirectPathAppTests.testA failed outside its own lines",
    ),
    # The test host's log split testA's own failed line: its failure cannot
    # be attributed, so nothing is rerun, not even the stalled testB.
    "a failed line split by the host's log": (
        [
            STARTED.format(cls="DirectPathAppTests", method="testA"),
            ERROR.format(cls="DirectPathAppTests", method="testA", message=ASSERTION),
            "Test Case '-[AvibeTests.DirectPathAppTests tes2026-11-26 20:11:28.258037+0000 Avibe[1:2] x",
            "tA]' failed (1.000 seconds).",
            *failure("testB", SLOW_LAUNCH),
            *ENDED,
        ],
        "a failure without its own failed line in DirectPathAppTests.testA",
    ),
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
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_RUN_ATTEMPT": "2",
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
    assert f"{tmp_path}/temp/AvibeTestDerivedData" in calls[1]
    assert "::warning title=Simulator stall::" in result.stdout
    # The lines that matched are printed, so the rerun shows its evidence.
    stalls = [SLOW_LAUNCH, UNRESPONSIVE, GPU_UNRESPONSIVE, SLOW_BOOTSTRAP]
    printed = result.stdout.split("Matched lines of ", 1)[1]
    evidence = (tmp_path / "temp/ios-simulator-stall.txt").read_text(encoding="utf-8")
    assert evidence.startswith("run attempt: 2\n")
    for line in RERUN[case]:
        assert (f"  {line}\n" in printed) == (line in stalls)
        assert (f"  {line}\n" in evidence) == (line in stalls)
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    for only in expected:
        name = only.split("/", 1)[1].replace("/", ".")
        assert f"- `{name}`: " in summary


@pytest.mark.parametrize("case", STANDS)
def test_any_other_failure_stands_without_a_rerun(tmp_path: Path, case: str) -> None:
    log, reason = STANDS[case]
    result, calls = run_step(tmp_path, log)

    assert result.returncode != 0
    assert len(calls) == 1
    assert "Not rerun:" in result.stderr
    assert reason in result.stderr
    assert not (tmp_path / "temp/ios-simulator-stall.txt").exists()
    assert not (tmp_path / "summary.md").exists()


def test_a_rerun_that_fails_again_fails_the_step(tmp_path: Path) -> None:
    result, calls = run_step(tmp_path, RERUN["slow launch"], rerun_exit=65)

    assert result.returncode != 0
    assert len(calls) == 2


def test_the_stall_evidence_is_uploaded_for_the_weekly_count() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ios-shell.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["simulator"]["steps"]
    upload, = [step for step in steps if step.get("name") == "Upload simulator stall evidence"]
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["name"] == "ios-simulator-stall"
    assert upload["with"]["path"] == "${{ runner.temp }}/ios-simulator-stall.txt"
    assert upload["with"]["if-no-files-found"] == "ignore"
    test_step, = [step for step in steps if step.get("name") == "Run XCTest on iPhone Simulator"]
    assert steps.index(test_step) < steps.index(upload)


def test_the_weekly_count_groups_reruns_by_iso_week() -> None:
    # Thursday 2026-11-26: this week starts Monday 11-23.
    created = [
        "2026-11-23T00:00:01Z",  # this week's first second
        "2026-11-26T19:07:51Z",
        "2026-11-22T23:59:59Z",  # last week's last second
        "2026-11-16T08:00:00Z",
        "2026-11-02T08:00:00Z",  # older than the weeks asked for
    ]
    assert weekly_counts(created, 3, date(2026, 11, 26)) == [
        (date(2026, 11, 23), 2),
        (date(2026, 11, 16), 2),
        (date(2026, 11, 9), 0),
    ]
