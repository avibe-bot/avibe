"""Bounded, observational metrics for the one-interpreter-per-file CI launcher."""

from __future__ import annotations

import heapq
import json
import os
from pathlib import Path
import sys
import threading
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


def linux_scheduler() -> dict | None:
    try:
        enabled = {"0": False, "1": True}[Path("/proc/sys/kernel/sched_schedstats").read_text().strip()]
        values = [int(value) for value in Path("/proc/thread-self/schedstat").read_text().split()]
        if len(values) != 3 or min(values) < 0:
            return None
        return {"enabled": enabled, "run_ns": values[0], "runqueue_ns": values[1]}
    except (OSError, ValueError, KeyError):
        return None


def linux_pressure(resource_name: str) -> dict[str, int] | None:
    try:
        totals = {}
        for line in Path(f"/proc/pressure/{resource_name}").read_text().splitlines():
            name, *fields = line.split()
            total = int(dict(field.split("=", 1) for field in fields)["total"])
            if total < 0 or name in totals:
                return None
            totals[name] = total
        required = {"some"} if resource_name == "cpu" else {"some", "full"}
        if not required <= totals.keys():
            return None
        # System-wide CPU "full" is undefined, even when the kernel emits zero.
        return {name: totals[name] for name in sorted(required)}
    except (OSError, ValueError, KeyError):
        return None


def _counter_seconds(before: int | float | None, after: int | float | None, units: int = 1) -> float | None:
    if before is None or after is None or after < before:
        return None
    return round((after - before) / units, 6)


def wait_observation(before: dict, after: dict) -> dict:
    same_thread = before["thread"] == after["thread"]
    first, last = before["scheduler"] or {}, after["scheduler"] or {}
    scheduler_enabled = first.get("enabled") is True and last.get("enabled") is True and same_thread
    return {
        "interval": "metrics_initialization_to_pytest_return",
        "wall_seconds": _counter_seconds(before["wall"], after["wall"]),
        "launcher_thread_cpu_seconds": (
            _counter_seconds(before["cpu"], after["cpu"]) if same_thread else None
        ),
        "linux_scheduler": {
            "scope": "launcher_thread",
            "enabled_at_start": first.get("enabled"),
            "enabled_at_end": last.get("enabled"),
            "run_seconds": _counter_seconds(first.get("run_ns"), last.get("run_ns"), 10**9)
            if scheduler_enabled else None,
            "runqueue_seconds": _counter_seconds(first.get("runqueue_ns"), last.get("runqueue_ns"), 10**9)
            if scheduler_enabled else None,
        },
        "linux_host_pressure": {
            "scope": "host",
            **{
                name: {
                    kind + "_seconds": _counter_seconds(
                        (before["pressure"][name] or {}).get(kind),
                        (after["pressure"][name] or {}).get(kind), 10**6,
                    )
                    for kind in (("some",) if name == "cpu" else ("some", "full"))
                }
                for name in ("cpu", "io", "memory")
            },
        },
    }


class FileMetrics:
    def __init__(self, test_file: str, started_at: float):
        self.test_file = test_file
        self.started_at = started_at
        self._clock = time.perf_counter
        self._thread_clock = time.thread_time
        self._thread_id = threading.get_ident
        self._wait_started = self._wait_snapshot()
        self.phases = dict.fromkeys(("collection", "setup", "call", "teardown"), 0.0)
        self.phase_counts = dict.fromkeys(self.phases, 0)
        self.slowest: list[tuple[float, str, str]] = []

    def _wait_snapshot(self) -> dict:
        return {
            "wall": self._clock(), "cpu": self._thread_clock(), "thread": self._thread_id(),
            "scheduler": linux_scheduler(),
            "pressure": {name: linux_pressure(name) for name in ("cpu", "io", "memory")},
        }

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
        wait_finished = self._wait_snapshot()
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
            "wait_observation": wait_observation(self._wait_started, wait_finished),
            "slowest_phases": [
                {"seconds": round(duration, 6), "phase": phase, "test": nodeid}
                for duration, phase, nodeid in sorted(self.slowest, reverse=True)
            ],
        }
        print("CI_TEST_METRICS " + json.dumps(payload, sort_keys=True), file=stream, flush=True)
