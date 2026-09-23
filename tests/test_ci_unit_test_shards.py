from __future__ import annotations

from pathlib import Path

import pytest

import json

from scripts.ci_unit_test_shards import (
    DEFAULT_WATCHDOG_MULTIPLIER,
    discover_unit_test_files,
    load_timings,
    main,
    plan_shards,
    watchdog_budget_seconds,
)


def _test_file(root: Path, name: str, *, tests: int = 1, lines: int = 1) -> Path:
    path = root / name
    path.write_text(
        ("def test_case():\n    pass\n" * tests) + ("# filler\n" * lines),
        encoding="utf-8",
    )
    return path


def test_plan_shards_balances_measured_durations(tmp_path: Path) -> None:
    files = [_test_file(tmp_path, f"test_{name}.py") for name in "abcd"]
    timings = {path.as_posix(): duration for path, duration in zip(files, [10, 9, 8, 7])}

    shards = plan_shards(files, 2, timings)

    assert [total for total, _ in shards] == [17, 17]
    assert {path for _, selected in shards for path in selected} == set(files)


def test_plan_shards_uses_structural_fallback_for_new_files(tmp_path: Path) -> None:
    measured = _test_file(tmp_path, "test_measured.py", tests=1, lines=1)
    new = _test_file(tmp_path, "test_new.py", tests=1, lines=4)

    shards = plan_shards(
        [measured, new],
        2,
        {measured.as_posix(): 10},
    )

    assert sorted(path for _, selected in shards for path in selected) == [measured, new]
    assert all(total > 0 for total, _ in shards)


def test_load_timings_ignores_malformed_values(tmp_path: Path) -> None:
    path = tmp_path / "timings.json"
    path.write_text(
        '{"durations_seconds": {"tests/test_ok.py": 3, "tests/test_bad.py": "x", "tests/test_bool.py": true}}',
        encoding="utf-8",
    )

    assert load_timings(path) == {"tests/test_ok.py": 3.0}


@pytest.mark.parametrize("shard_total", [0, -1])
def test_plan_shards_rejects_invalid_shard_total(tmp_path: Path, shard_total: int) -> None:
    with pytest.raises(ValueError, match="shard_total"):
        plan_shards([_test_file(tmp_path, "test_one.py")], shard_total)


def test_measured_plan_runs_every_discovered_file_exactly_once() -> None:
    files = discover_unit_test_files()
    shards = plan_shards(files, 6, load_timings())

    assert sorted(path for _, selected in shards for path in selected) == files


def test_watchdog_budget_scales_with_the_file_own_recorded_cost(tmp_path: Path) -> None:
    slow = _test_file(tmp_path, "test_slow.py")
    quick = _test_file(tmp_path, "test_quick.py")
    timings = {slow.as_posix(): 74.0, quick.as_posix(): 2.0}

    slow_budget, slow_baseline = watchdog_budget_seconds(slow, timings, floor_seconds=300)
    quick_budget, quick_baseline = watchdog_budget_seconds(quick, timings, floor_seconds=300)

    # The slow file is the one a fixed budget squeezes: 74s against 300s leaves
    # it barely 4x of headroom, which a merely slow runner can eat. Its budget
    # is a multiple of its own cost, so slowness alone no longer reads as a hang.
    assert slow_budget == int(74 * DEFAULT_WATCHDOG_MULTIPLIER)
    assert slow_baseline == 74.0
    # A quick file would be *cut* by the same multiple, so the floor holds.
    assert quick_budget == 300
    assert quick_baseline == 2.0


def test_watchdog_budget_keeps_the_floor_for_files_with_no_recorded_cost(tmp_path: Path) -> None:
    unknown = _test_file(tmp_path, "test_unknown.py", tests=200, lines=5000)

    budget, baseline = watchdog_budget_seconds(unknown, {}, floor_seconds=300)

    # The planner's structural estimate is a relative weight, not seconds.
    # Multiplying it would hand an arbitrary deadline to exactly the files
    # nothing is known about, however large the estimate grows.
    assert budget == 300
    assert baseline is None


def test_main_emits_budgets_only_when_a_floor_is_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "tests").mkdir()
    measured = _test_file(tmp_path / "tests", "test_measured.py")
    _test_file(tmp_path / "tests", "test_new.py")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ci_unit_test_timings.json").write_text(
        json.dumps({"durations_seconds": {"tests/test_measured.py": 50.0}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert measured.is_file()

    assert main(["0", "1"]) == 0
    plain = capsys.readouterr().out.splitlines()
    # Callers that only want a file list keep reading one, unchanged.
    assert plain == ["tests/test_measured.py", "tests/test_new.py"]

    assert main(["--budget-floor=10", "0", "1"]) == 0
    budgeted = [line.split("\t") for line in capsys.readouterr().out.splitlines()]
    assert budgeted == [
        ["tests/test_measured.py", str(int(50 * DEFAULT_WATCHDOG_MULTIPLIER)), "50"],
        # Blank baseline is how the launcher knows to print the legacy line.
        ["tests/test_new.py", "10", ""],
    ]


def test_main_rejects_a_budget_floor_that_is_not_a_positive_number_of_seconds() -> None:
    assert main(["--budget-floor=0", "0", "1"]) == 2
