#!/usr/bin/env python3
"""Decide whether a failed XCTest run failed only through the simulator's stalls.

On GitHub's hosted xcode-27 runner the iOS 27.2 simulator stalls WebKit's own
processes now and then: a WebContent or GPU process takes 30-40 s to launch, or
WebKit reports it unresponsive and kills it, or the whole simulator says nothing
for two minutes. A test whose wait runs through such a stall fails at its own
bound, and the same test on the same head passes on the next run.

Given the log of one ``xcodebuild test`` run, this writes the
``-only-testing:`` arguments that run every failed test once more to ``--out``,
one per line, and exits 0,
only when the run completed, no test process crashed or was restarted, at most
``--max-tests`` tests failed, and every failed test's own lines show one of
these stalls. Anything else exits 1 with the reason: that failure stands.

Each stalled test is written as a ``::warning::`` annotation, followed by the
log lines that matched, so a test that passes only on its second run still
shows on the run with its evidence.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# A process WebKit launched for the App took this long to come up. A healthy
# launch on the hosted runner takes 1-17 s; the stalls behind failed tests
# took 28-41 s.
LAUNCH_STALL_SECONDS = 20.0
# The test fixture's App took this long to settle its account bootstrap. A
# healthy one settles in 1-9 s; the stalled ones took 120-126 s.
BOOTSTRAP_STALL_SECONDS = 60.0

_CASE = re.compile(
    r"Test Case '-\[(?P<target>\w+)\.(?P<cls>\w+) (?P<method>\w+)\]' "
    r"(?P<event>started|passed|failed|skipped)"
)
_LAUNCH = re.compile(
    r"\b(?P<process>WebContent|GPU|Networking) process \(0x[0-9a-f]+\) "
    r"took (?P<seconds>\d+(?:\.\d+)?) seconds to launch"
)
_UNRESPONSIVE = re.compile(r"\b(?P<process>\w+)ProcessProxy::didBecomeUnresponsive\b")
_BOOTSTRAP = re.compile(
    r"\[iOS fixture\] stage=bootstrap settled, elapsed=(?P<seconds>\d+(?:\.\d+)?) seconds"
)
_RUN_ENDED = "Test Suite 'All tests' failed"
_RESTARTED = "Restarting after unexpected exit, crash, or test timeout"


@dataclass
class _Case:
    target: str
    cls: str
    method: str
    stalls: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.cls}.{self.method}"

    @property
    def only_testing(self) -> str:
        return f"-only-testing:{self.target}/{self.cls}/{self.method}"


def _stall(line: str) -> str | None:
    launch = _LAUNCH.search(line)
    if launch and float(launch["seconds"]) >= LAUNCH_STALL_SECONDS:
        return f"{launch['process']} process took {float(launch['seconds']):.1f} s to launch"
    unresponsive = _UNRESPONSIVE.search(line)
    if unresponsive:
        process = "WebContent" if unresponsive["process"] == "Web" else unresponsive["process"]
        return f"WebKit's {process} process became unresponsive"
    bootstrap = _BOOTSTRAP.search(line)
    if bootstrap and float(bootstrap["seconds"]) >= BOOTSTRAP_STALL_SECONDS:
        return f"the App's bootstrap settled after {float(bootstrap['seconds']):.1f} s"
    return None


def stalled_failures(lines: list[str], max_tests: int) -> tuple[list[_Case], str | None]:
    """Return the failed tests and ``None``, or ``[]`` and why they stand."""
    current: _Case | None = None
    failed: list[_Case] = []
    ended = False
    for line in lines:
        if _RESTARTED in line:
            return [], "a test process crashed or timed out and was restarted"
        if _RUN_ENDED in line:
            ended = True
        case = _CASE.search(line)
        if case:
            if case["event"] == "started":
                current = _Case(case["target"], case["cls"], case["method"])
            elif case["event"] == "failed":
                name = f"{case['cls']}.{case['method']}"
                if current is None or current.name != name:
                    return [], f"{name} failed outside its own lines"
                failed.append(current)
                current = None
            else:
                current = None
            continue
        if current is not None:
            stall = _stall(line)
            if stall:
                current.evidence.append(line.strip())
                if stall not in current.stalls:
                    current.stalls.append(stall)
    if not ended:
        return [], "the run did not complete"
    if not failed:
        return [], "no test failed: the run failed for another reason"
    if len(failed) > max_tests:
        return [], f"{len(failed)} tests failed, more than one stall explains"
    unexplained = [case.name for case in failed if not case.stalls]
    if unexplained:
        return [], "no simulator stall during " + ", ".join(unexplained)
    return failed, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path, help="the xcodebuild test log")
    parser.add_argument("--out", type=Path, required=True, help="where the arguments go")
    parser.add_argument("--max-tests", type=int, default=3)
    args = parser.parse_args(argv)

    lines = args.log.read_text(encoding="utf-8", errors="replace").splitlines()
    failed, reason = stalled_failures(lines, args.max_tests)
    if reason:
        print(f"Not rerun: {reason}.", file=sys.stderr)
        return 1
    for case in failed:
        print(
            f"::warning title=Simulator stall::{case.name} failed through a "
            f"simulator stall ({'; '.join(case.stalls)}) and runs once more."
        )
        print(f"Matched lines of {case.name}:")
        for line in case.evidence:
            print(f"  {line}")
    args.out.write_text("".join(f"{case.only_testing}\n" for case in failed), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
