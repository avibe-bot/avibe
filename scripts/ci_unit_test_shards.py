"""Plan isolated unit-test files across CI shards by measured duration."""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Iterable, Mapping

DEFAULT_TEST_ROOT = Path("tests")
DEFAULT_TIMINGS_PATH = Path("scripts/ci_unit_test_timings.json")

#: How much slower than its own recorded duration a file may run before the
#: per-file watchdog treats it as stalled rather than merely unlucky. A single
#: wall-clock budget cannot tell those apart across a suite whose files span
#: three orders of magnitude, so the budget is expressed as a multiple of what
#: the file itself has actually cost.
DEFAULT_WATCHDOG_MULTIPLIER = 8.0


def discover_unit_test_files(root: Path = DEFAULT_TEST_ROOT) -> list[Path]:
    """Return deterministic unit-test files, excluding Docker-backed e2e tests."""
    return sorted(
        path
        for path in root.rglob("test_*.py")
        if "tests/e2e/" not in path.as_posix()
    )


def structural_weight(path: Path) -> int:
    """Estimate a new file's cost before it has a timing sample."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    line_count = text.count("\n") + 1
    test_count = len(re.findall(r"^\s*(?:async\s+def|def)\s+test_", text, re.MULTILINE))
    class_count = len(re.findall(r"^\s*class\s+Test", text, re.MULTILINE))
    return max(1, line_count + (test_count * 20) + (class_count * 60))


def load_timings(path: Path = DEFAULT_TIMINGS_PATH) -> dict[str, float]:
    """Load a timing snapshot, tolerating a missing or stale local snapshot."""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    durations = payload.get("durations_seconds", payload) if isinstance(payload, dict) else {}
    if not isinstance(durations, dict):
        return {}
    result: dict[str, float] = {}
    for file_path, duration in durations.items():
        if not isinstance(file_path, str) or isinstance(duration, bool):
            continue
        try:
            value = float(duration)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            result[file_path] = max(1.0, value)
    return result


def _timing_scale(files: Iterable[Path], timings: Mapping[str, float]) -> float:
    ratios = [
        timings[path.as_posix()] / structural_weight(path)
        for path in files
        if path.as_posix() in timings
    ]
    return statistics.median(ratios) if ratios else 1.0


def expected_seconds(
    files: Iterable[Path],
    timings: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Cost per file: its measured duration, else a scaled structural estimate."""
    file_list = sorted(files)
    timing_map = timings or {}
    scale = _timing_scale(file_list, timing_map)
    return {
        path.as_posix(): max(1.0, float(timing_map.get(path.as_posix(), structural_weight(path) * scale)))
        for path in file_list
    }


def watchdog_budget_seconds(
    path: Path,
    timings: Mapping[str, float] | None = None,
    *,
    floor_seconds: int,
    multiplier: float = DEFAULT_WATCHDOG_MULTIPLIER,
) -> tuple[int, float | None]:
    """Return the watchdog budget for one file and the baseline it came from.

    Only a *measured* duration raises the budget. The structural estimate the
    planner falls back on is a relative weight, not seconds, so multiplying it
    would hand an arbitrary deadline to exactly the files nothing is known
    about; those keep the floor, which is what the fixed budget always was.
    """
    baseline = (timings or {}).get(path.as_posix())
    if baseline is None or baseline <= 0:
        return floor_seconds, None
    return max(floor_seconds, math.ceil(baseline * multiplier)), baseline


def plan_shards(
    files: Iterable[Path],
    shard_total: int,
    timings: Mapping[str, float] | None = None,
) -> list[tuple[float, list[Path]]]:
    """Assign the heaviest remaining file to the lightest shard (LPT)."""
    if shard_total < 1:
        raise ValueError("shard_total must be at least 1")
    file_list = sorted(files)
    weights = expected_seconds(file_list, timings)
    weighted_files = [(weights[path.as_posix()], path) for path in file_list]

    shards: list[tuple[float, list[Path]]] = [(0.0, []) for _ in range(shard_total)]
    for weight, path in sorted(weighted_files, key=lambda item: (-item[0], item[1].as_posix())):
        target = min(
            range(shard_total),
            key=lambda index: (shards[index][0], len(shards[index][1]), index),
        )
        total, selected = shards[target]
        shards[target] = (total + weight, [*selected, path])
    return shards


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    budget_floor: int | None = None
    for index, value in enumerate(args):
        if value.startswith("--budget-floor="):
            budget_floor = int(value.removeprefix("--budget-floor=") or 0)
            del args[index]
            break
    if len(args) != 2 or any(not value.isdigit() for value in args):
        print(
            "Usage: ci_unit_test_shards.py [--budget-floor=SECONDS] SHARD_INDEX SHARD_TOTAL",
            file=sys.stderr,
        )
        return 2
    shard_index, shard_total = (int(value) for value in args)
    if shard_total < 1 or shard_index >= shard_total:
        print("Shard index must be less than a positive shard total.", file=sys.stderr)
        return 2
    if budget_floor is not None and budget_floor < 1:
        print("Budget floor must be a positive number of seconds.", file=sys.stderr)
        return 2

    files = discover_unit_test_files()
    timings = load_timings()
    shards = plan_shards(files, shard_total, timings)
    selected = sorted(shards[shard_index][1])
    print(
        f"Planned {len(files)} unit test file(s) across {shard_total} shard(s); "
        f"shard {shard_index} has {len(selected)} file(s), "
        f"estimated weight {shards[shard_index][0]:.0f}s "
        f"({len(timings)} historical timing samples).",
        file=sys.stderr,
    )
    for path in selected:
        if budget_floor is None:
            print(path.as_posix())
            continue
        budget, baseline = watchdog_budget_seconds(path, timings, floor_seconds=budget_floor)
        # Tab-separated so the launcher reads the budget it must arm without
        # paying a second interpreter start per file to recompute it.
        print(f"{path.as_posix()}\t{budget}\t{'' if baseline is None else format(baseline, '.0f')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
