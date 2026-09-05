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
