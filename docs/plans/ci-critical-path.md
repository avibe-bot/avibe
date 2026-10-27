# CI Critical Path

## Baseline

Read at master `9993ce41ce3669e5d05ae167564daffb6cb4cbc5`.
Successful lint runs on September 5, 2026:

| Run | Source | Wall time | Artifact build | Install regression | Slowest Python shard |
| --- | --- | --- | --- | --- | --- |
| [33952795137](https://github.com/avibe-bot/avibe/actions/runs/33952795137) | `9993ce41ce` | 13m24s | 8m00s | 5m17s | 9m35s |
| [33951672367](https://github.com/avibe-bot/avibe/actions/runs/33951672367) | `d5bd8d1cda` | 11m46s | 7m26s | 4m13s | 9m23s |

The artifact build followed by install regression is the critical path.
In the first run, UI validation and tests consumed approximately four minutes
before the production bundle could be built. Unit dependency installation took
two to three seconds; adding more dependency caches is not the first lever.

## Changes and Invariants

- UI validation runs independently from production artifact creation. Every
  existing theme, lint, test-typecheck, test, and production-build command remains
  gated. The existing `unit-tests` aggregate requires both Python shards and
  `ui-checks` to succeed, including fail-closed handling of skipped/cancelled jobs.
  Artifact consumers still require the real production artifact build.
- Packaged upgrade tests build the two core/Memory version pairs once per module,
  reducing sixteen wheel builds to four. Each case copies those immutable inputs
  into its own wheelhouse and installs into its own virtual environment. Deleting
  Memory wheels in the missing-package case cannot affect another test or the
  shared inputs. All upgrade and optional-import assertions remain unchanged.
- The existing six-shard planner uses a refreshed timing snapshot containing all
  396 files from run 33952795137. No planner algorithm or test selection changes.
  Every discovered file, including newly added files without timing samples, must
  appear in exactly one shard; unknown files retain the structural estimate.

Replaying the measured durations with the refreshed assignment gives about
7m23s per shard instead of a 9m24s slowest test step. As a holdout, run
33951672367 gives 6m42s to 7m14s with that same assignment. These are scheduling
estimates, not a promise about runner speed or queue time. Compare actual PR job
timestamps after the change before claiming an end-to-end improvement.

## Evidence and Boundaries

- Workflow contract tests execute the aggregate shell gate across all dependency
  outcomes and verify that UI checks cannot be bypassed by the split.
- The real packaged upgrade tests cover core-only status, paired upgrades,
  core-only upgrades, and refusal when Memory is missing. Their fixture isolation
  also has a focused mutation test. Related scenarios: MEMORY-INDEP-021 and
  MEMORY-INDEP-024/025.
- Full UI checks and builds remain required; so do package distribution/sdist
  contracts, released migration guards, pinned Memory runtime contracts, Docker
  install/upgrade regressions, and Windows installation smoke tests.
- There is one additional lightweight Node setup/npm install for the independent
  UI job. No new dependencies, services, path-based skips, or reduced coverage.
- Timing snapshots remain advisory and may age. Refresh from complete successful
  shard logs, preserve the source run/commit, and validate against another run
  before changing the snapshot again. Automatic refresh is outside this change.

## October 2026: Runner Lanes and Browser Packages

### Baseline

Sample: the 45 latest successful `lint` runs up to master `f225b7a0b`
(39 first attempts). Median elapsed was 18.7 minutes; the 90th percentile was
64.6 minutes. Job setup was small: checkout, uv, and npm restores each took
seconds and every dependency cache hit.

- `ui-browser-checks` was the longest job in 18 of 39 first attempts.
  It took a median of 14.7 minutes, running ten suites serially with one worker.
  Each suite also spent about 30 seconds in `tsc` and Vite startup.
- The slowest of the eight unit shards finished after 11 to 14 minutes.
  The suite had grown to 616 files and about 4,900 launcher seconds,
  and 77 files were missing from the timing snapshot.
  Per-file `CI_TEST_METRICS` show that 86% of file wall time was process CPU
  on a single core of a four-vCPU runner.
  Process startup and shutdown accounted for about 13%.
- Slow runs had two causes. In 3 of 45 runs, `playwright install --with-deps`
  downloaded 119 MB of WebKit packages from the Ubuntu mirror in 13, 22, and 44
  minutes; Playwright's CDN and `apt-get update` stayed fast. The other cause was
  queue saturation. The organization has 20 hosted slots, and one run requests
  33 to 39 jobs. A burst of five Dependabot PRs took 65 to 75 minutes,
  with up to 48 minutes of shard queueing.

### Changes and Invariants

- Each unit runner executes two planned lanes concurrently through
  `scripts/ci_unit_test_lanes.py`. Shard `i` of `N` runs virtual shards `2i`
  and `2i+1` of `2N`. The existing planner and launcher are unchanged, so
  every discovered file still runs exactly once, in its own interpreter, with
  its own watchdog. Any failing lane fails the shard. The lanes are child
  processes, not shell background jobs, so test processes keep Python's
  default SIGINT handling.
- The timing snapshot is refreshed from three successful master runs and covers
  all 615 files.
- Browser suites run in two matrix lanes that are balanced by measured suite time.
  Each suite still runs exactly once with the same arguments. The `unit-tests`
  gate still requires the `ui-browser-checks` result, which fails if either
  lane fails.
- apt keeps Playwright's system packages in a directory that is cached per
  runner image and Playwright version. apt still resolves, verifies, and
  installs every package, and downloads any package that is missing.

No tests, required checks, watchdogs, or runner labels are removed.
Measured results for the pull request's own runs are recorded in the PR.
