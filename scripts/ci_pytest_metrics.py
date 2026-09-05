"""Bounded, observational metrics for the one-interpreter-per-file CI launcher."""

from __future__ import annotations

import heapq
import json
import os
from pathlib import Path
import sys
import time

import pytest

try:
    import resource
except ImportError:  # The shell launcher is POSIX, but importing this helper is portable.
    resource = None


def process_usage() -> dict | None:
    if resource is None:
        return None
    result = {}
    for name, who in (("self", resource.RUSAGE_SELF), ("waited_children", resource.RUSAGE_CHILDREN)):
        usage = resource.getrusage(who)
        result[name] = {
            "user_cpu_seconds": round(usage.ru_utime, 6),
            "system_cpu_seconds": round(usage.ru_stime, 6),
            "input_blocks": usage.ru_inblock,
            "output_blocks": usage.ru_oublock,
            "voluntary_context_switches": usage.ru_nvcsw,
            "involuntary_context_switches": usage.ru_nivcsw,
        }
    # getrusage reports bytes on macOS and KiB on Linux. This is a peak, not a delta.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["self"]["peak_rss_bytes"] = peak * (1 if sys.platform == "darwin" else 1024)
    return result


def linux_io() -> dict[str, int] | None:
    try:
        values = {}
        for line in Path("/proc/self/io").read_text().splitlines():
            name, value = line.split(":", 1)
            values[name] = int(value)
        return values
    except (OSError, ValueError):
        return None


class FileMetrics:
    def __init__(self, test_file: str, started_at: float):
        self.test_file = test_file
        self.started_at = started_at
        self._clock = time.perf_counter
        self.phases = dict.fromkeys(("collection", "setup", "call", "teardown"), 0.0)
        self.phase_counts = dict.fromkeys(self.phases, 0)
        self.slowest: list[tuple[float, str, str]] = []

    def _record(self, phase: str, started_at: float, nodeid: str = "") -> None:
        duration = self._clock() - started_at
        self.phases[phase] += duration
        self.phase_counts[phase] += 1
        if nodeid:
            heapq.heappush(self.slowest, (duration, phase, nodeid[:512]))
            if len(self.slowest) > 5:
                heapq.heappop(self.slowest)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection(self):
        started = self._clock()
        try:
            return (yield)
        finally:
            self._record("collection", started)

    # Time phase execution, not reports: subtest reports overlap their parent call.
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        started = self._clock()
        try:
            return (yield)
        finally:
            self._record("setup", started, item.nodeid)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        started = self._clock()
        try:
            return (yield)
        finally:
            self._record("call", started, item.nodeid)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item):
        started = self._clock()
        try:
            return (yield)
        finally:
            self._record("teardown", started, item.nodeid)

    def emit(self, stream, exit_code: int) -> None:
        wall = self._clock() - self.started_at
        affinity = None
        if hasattr(os, "sched_getaffinity"):
            try:
                affinity = len(os.sched_getaffinity(0))
            except OSError:
                pass  # Sandboxes can deny optional process counters.
        payload = {
            "schema_version": 1,
            "file": self.test_file,
            "boundary": "pytest_returned_before_interpreter_shutdown",
            "exit_code": exit_code,
            "wall_seconds": round(wall, 6),
            "phase_seconds": {name: round(value, 6) for name, value in self.phases.items()},
            "phase_counts": self.phase_counts,
            "outside_phases_seconds": round(wall - sum(self.phases.values()), 6),
            "process_usage": process_usage(),
            "linux_proc_io": linux_io(),
            "cpu_affinity_count": affinity,
            "slowest_phases": [
                {"seconds": round(duration, 6), "phase": phase, "test": nodeid}
                for duration, phase, nodeid in sorted(self.slowest, reverse=True)
            ],
        }
        print("CI_TEST_METRICS " + json.dumps(payload, sort_keys=True), file=stream, flush=True)
