from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci_unit_test_shards import discover_unit_test_files, load_timings, plan_shards


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
