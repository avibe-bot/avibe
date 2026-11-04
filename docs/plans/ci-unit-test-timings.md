# Unit-test timing snapshot refresh (2026-11-05)

## Scope

A data-only refresh of `scripts/ci_unit_test_timings.json`. The planner
(`scripts/ci_unit_test_shards.py`), the launcher (`scripts/ci_unit_tests.sh`),
the two-lane runner, test discovery, the 300 s watchdog floor and multiplier,
the workflow and every test file stay unchanged. Every discovered file still
runs exactly once, in its own interpreter.

## Why

The previous snapshot came from serial single-lane shards before the two-lane
runners (#3019). It has 615 entries; master now discovers 662 files. The 47
missing files fall back to the planner's scaled structural estimate. One of
them, `tests/test_mcp_hub_project_scope.py`, was estimated at 9 s but took
64–110 s across eight successful master runs. LPT put it in virtual shard 2
(shard 1/8, lane 0) next to `tests/test_ui_api.py` (92 s recorded, 75–125 s
measured). That lane is
often the longest Python lane: in master run 41910353255 (`9ea2dd696`) it ran
459 s of file work while the shortest lane ran 202 s, and the
`unit-tests (1/8)` job took 483 s.

## Method

This is the method behind the previous snapshots. The repository has no
generator script.

- Source: the launcher's `Finished <file> in Ns with exit code C.` lines from
  all eight `unit-tests (i/8)` jobs of successful master `lint` runs. Each run
  must list every discovered file exactly once, with exit code 0 or 5 (5 means
  nothing was collected), and no duplicates.
- Training runs: 41906728101 (`626d91813`), 41870088755 attempt 3
  (`090d00aa1`; attempts 1 and 2 failed only because a macOS
  `web-fetch-runtime` job never got a runner) and 41848648083 (`8a88a53db`).
  Each ran the same 662 files that `origin/master` `9ea2dd696` discovers.
- Value: the median of the three samples, floored at 1 s as before. Keys are
  sorted and the schema (`source`, `durations_seconds`) is unchanged.
- Entries: 662 measured entries, one for each discovered file. No entry is
  stale. The 47 previously missing files now have measured values. No file
  needed an invented value or the structural fallback.

## Predicted versus replayed balance

All figures are seconds of launcher file work per virtual shard (lane). They
use the current 662-file inventory and LPT across 16 virtual shards. The
"Old plan" columns use the previous snapshot, and the "New plan" columns use
the refreshed one. Each row replays both plans against the same per-file
sample. A runner's file work is the longer of its two lanes.

| Sample | Old plan: longest | Old plan: spread | New plan: longest | New plan: spread |
| --- | ---: | ---: | ---: | ---: |
| Planner's own weights (model) | 314 | 1 | 315 | 1 |
| Training median (in-sample) | 423 | 208 | 315 | 5 |
| Holdout 41910353255 (`9ea2dd696`) | 459 | 257 | 307 | 63 |
| Holdout 41845464337 (`f8be07f4c`) | 404 | 213 | 330 | 69 |
| Holdout 41841551366 (`6b895fcb8`) | 374 | 124 | 339 | 49 |
| Holdout 41833664070 (`d41e1878a`) | 389 | 176 | 359 | 94 |
| Holdout 41825371789 (`3a499d9d6`) | 384 | 180 | 341 | 67 |

The previous planner predicted 314 s for every lane. The runs it actually
scheduled did 374–459 s on their longest lane. For the five holdouts, the old
plan's replayed longest lane equals the longest lane observed in that run's
logs, which validates the replay. Across every 3-run subset of the seven
earlier successful master runs, each evaluated on the four runs it excludes
(140 pairs), the refreshed plan's longest lane has a median of 345 s and a
maximum of 377 s. The old plan has a median of 397 s and a maximum of 458 s.
The refreshed plan is better in every pair (minimum 7 s, median 51 s).

These are replays, not CI observations. Runner speed varies by about 10%
between runs. In the samples, a shard job's wall time was about 25 s more than
its longest lane. The PR's own CI run is the first observation of the
refreshed plan.

## Watchdog budgets

Each file's watchdog budget is `max(300, ceil(8 × recorded))`, so new data
changes 22 budgets. The formula and floor are unchanged. Thirteen budgets
rise; for example, `test_mcp_hub_project_scope.py` gets 800 s instead of the
300 s floor that applied while it had no entry. Nine fall. Every lowered
budget is still at least 5.9× the file's slowest launcher time in the eight
successful master runs above; `test_show_pages.py` falls from 480 s to the
300 s floor after a slowest run of 20 s. The lowest ratio of budget to slowest
run across all files rises from 2.7× (`test_mcp_hub_project_scope.py` on the
floor) to 5.9×. The largest raised budgets, 896 s, apply to `test_ui_api.py`
and `test_scheduled_tasks.py`.

## Expected effect and boundary

The refresh balances Python lanes. On its own it is not expected to shorten
the required `unit-tests` gate. That gate also waits for the browser lanes,
which finish about ten minutes after the run starts. The slowest unit shard
finishes about eight minutes after the start, followed by the
`scenario-evidence` job. The refresh removes that unit shard from the near
critical path. Refresh again from complete successful master runs once enough
new or changed files are missing from, or diverge from, the snapshot.
Automatic refresh is out of scope.
