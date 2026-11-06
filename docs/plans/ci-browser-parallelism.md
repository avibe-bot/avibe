# Browser and design CI parallelism

## Current policy (2026-11-04)

PR #3232 delivered the parallelism change below. Its successful full CI run
`41829479607` completed in 10:08, with every original browser command and
all 1,098 frame/theme pairs on its tested source preserved.

The owner subsequently chose to **remove full design captures from routine
PR and master CI**. The three screenshot jobs and their required-aggregate
references are removed, not converted to skipped or advisory checks. The four
browser lanes, every existing browser command, design type checking and all
other required gates remain. `design:shot` and `design:export` remain manual
tools, including the existing optional shard selector; there is no new nightly
job. The rationale and accepted loss of whole-catalog render-smoke coverage are
in [Design frames](design-frames.md#ci-boundary).

### Every e2e suite type-checks (2026-11-06)

A browser lane type-checks only the suites it runs (`scripts/e2e-suite.mjs`).
Suites outside the lanes were type-checked by nothing, and one broke unnoticed
when a shared component gained a required prop. `ui-checks (static)` now runs
`npm run typecheck:e2e`, which type-checks every suite under `ui/e2e/`. It
fails when a directory with specs has no `tsconfig.json`, so a new suite cannot
be missed. In its first CI run (`42190302240`) the 21 checks took 2:30, four at
a time (20–40 s each; 30 s in all locally). That brought the static part from
2:18–2:34 to 5:10, still under the tests part (8:40) and every browser lane
(9:31–11:44) of the same run.

`test:chat-paging` joined lane 2, the emptiest (its suites summed 377 s and
459 s in master runs `42165094179` and `42167111540`). It took 52 s on CI.
Suites whose browser cases no lane runs:

| Suite | Local wall time | Where its cases run |
| --- | --- | --- |
| model-catalog | 409 s (622 s on CI) | nightly `model-hub-ui-regression` |
| model-provider | 28 s (49 s on CI) | nightly `model-hub-ui-regression` |
| citations | 24 s | nowhere |
| queue-attachments | 69 s | nowhere |
| session-dot | 20 s | nowhere |
| web-fetch | 3 s, no npm script | nowhere (manual screenshots) |

Local times are whole npm commands on an Apple Silicon Mac. On the three lane
suites measured both ways, CI took 1.3–2.0 times as long.

The sections below record the original PR #3232 contract and measurements, not
a requirement to restore its screenshot gate. Removing parallel captures reduces
runner work; it does not promise to subtract their duration from the browser
critical path.

## Original change contract

Reduce the critical path without reducing the checks run on a pull request.
The existing browser commands, selected specs, Playwright configurations,
retries, type checks and page isolation remain unchanged. Run those commands
once across four independent runners, and render every selected design
frame/theme pair once across three separate screenshot runners. The existing
required `unit-tests` aggregate waits for both matrices and fails closed;
only proven docs-only changes may skip them. Non-UI jobs are unchanged.

No worker-count, assertion, timeout, dependency, runner-class, release,
permission or scheduled-test changes are part of this work.

## Baseline and proposed layout

Run `41810827956` on `f47433a7` (2026-11-04) took 32:08 end to end.
The browser lanes took 22:02 and 30:25, including setup. Lane 2 ended
with 17:01 of `design:shot`: 1,084 successful captures (542 frames, each
in light and dark). The next run, `41814595510` on `734485c8`, took 33:23.
Across six recent successful master runs the observed total ranged from 24:42
to 33:23 (median 29:28.5); browser lane 2 was longest in every sample.
These are observational measurements, not a controlled same-source benchmark
or a promise that parallel execution scales linearly.

Four browser lanes, balanced by the first run's observed command wall times:

| Lane | Commands | Estimated execution |
| --- | --- | --- |
| 1 | pet-mini, chat-interactions, inbox-return | 8:13 |
| 2 | five workbench-general specs, built composer-mention-picker, project-order, chat-paging (added later, about 1 minute) | 8:08 |
| 3 | three onboarding-fidelity specs, home-media, pdf-preview, motion (added later, about 1 minute) | 8:10 |
| 4 | vault-integrity, workbench geometry, badge-triggers, chat-selection, voice-shortcut, chat-scroll (added later, about 1 minute) | 7:52 |

Suites remain serial inside each runner. Existing per-suite TypeScript checks
already run alongside Playwright at lower priority; preserve that behavior.
Three screenshot runners partition the flattened frame/theme list round-robin.
Each still uses the existing bounded concurrency and a fresh page per capture.
The target is roughly 10–15 minutes for complete CI, subject to real PR results,
runner availability and the unchanged non-UI critical paths.

## Screenshot CLI and consumers

- Without `--shard`, selection and output paths are unchanged.
- `--shard INDEX/TOTAL` is one-based and partitions **after** prefix/theme
  selection. Invalid, out-of-range or empty shards fail, never pass with no work.
- The default still renders every declared theme. An explicit `--theme` keeps
  the existing override semantics, even for a theme not declared by that frame.
- Sharding is for independent CI outputs. Reject combining it with `--manifest`
  rather than introducing concurrent read/modify/write races in a shared index.
  Ordinary `design:export` and unsharded manifest merging remain unchanged.
- Existing per-capture JSON records remain intact. Add one stderr summary with
  shard, selected/completed counts, elapsed wall time and verdict. Browser job
  timing includes each full npm command, including built-app preparation.

## Validation ownership

- Extend `tests/test_ci_pipeline.py` to execute the actual matrix command lists
  with a recording npm stub. Compare against the pre-change command inventory:
  omitted/duplicated specs, changed flags or swallowed failures must fail.
  Existing UI scheduling tests do not pin the browser selection.
- Extend the existing required-gate test (which executes the real shell gate)
  to cover the screenshot producer alongside each browser/result producer.
  Missing, cancelled, failed and unexpectedly skipped results must fail.
- Add focused Vitest coverage at the screenshot selection boundary. Use an
  explicit unequal-theme fixture and literal expected frame/theme pairs;
  prove complete/disjoint shards, prefix/theme compatibility, and fail-closed
  invalid/empty selection. Previously there was no sharding to exercise.
- Run focused Python/Vitest checks, changed Python Ruff, and `npm run build`.
  Then use the real PR CI logs for wall time and complete coverage evidence.

## CI-discovered test synchronization correction

The first two CI attempts completed in 9:48 and 8:59, but failed in existing
Computer Use tests; neither is a green full-CI benchmark. All UI checks passed
both times. The shared `_run_python_deadline_script` helper assumed Node could
initialize and produce the asserted runtime state inside a 250 ms total budget.
Attempt 1 lacked diagnostics; attempt 2 lacked runtime state in all three
running-context cases. A test-owned delayed Node launcher reproduced both
failure shapes without changing production code.

The correction is confined to `tests/test_computer_script_sdk.py`. It separates
startup-budget enforcement from tests that require an already-running state:

| Helper consumer | Required evidence / treatment |
| --- | --- |
| Continuous console diagnostics | Child's actual persisted diagnostic tail before timing termination |
| Detached descendant stdout | Marker the fixture child writes after spawning its descendant |
| Busy loop / unresolved Promise | Marker the fixture child writes on entering the tested body |
| In-flight clipboard | Actual request received by the fake loopback controller |
| Last screenshot at deadline | Actual screenshot context plus the in-flight click request |
| Oversized result | Existing 5 s result/protocol path, unchanged |
| Finalize recovery (three cases) | Existing 5 s outcome path and exact step/screenshot assertions, unchanged |
| Referenced-screenshot retention | Existing 5 s outcome path and artifact assertions, unchanged |

For the running-state cases only, a test wrapper around the real `Popen.wait`
waits for that evidence under a bounded readiness watchdog, then calls the real
wait with the **unchanged timeout argument supplied by the parent**. It never
manufactures state, diagnostics, screenshots or failure envelopes. Existing
elapsed/cleanup assertions measure termination after the required state is
established, rather than accidental Node startup speed. An early exit or missing
readiness fails clearly; a dedicated negative case proves a non-starting child
cannot turn into a pass.

A separate blocked-launcher case retains the real 250 ms total deadline,
asserts the exact missing-runtime-state timeout envelope and prompt child reap,
and requires no startup-speed assumption. The existing clock-controlled
single-budget test still proves open work reduces the budget passed to Node and
close. No production seam, global deadline increase, relaxed failure payload,
removed assertion or extra CI retry was introduced.

Local validation of this correction: full SDK file **77 passed**; the nine
focused deadline/cleanup cases passed three consecutive runs. A diagnostic-only
300 ms delayed-launch reproducer that failed the pre-fix running-state cases
passed all four affected cases in three consecutive runs after the correction.
The delay is not part of the committed test harness.

## Delivery status

Local validation passed: 63 CI-pipeline tests, 56 docs-classification/guidance
tests, 22 focused Vitest tests (selection and the existing suite runner),
changed-file Ruff and the production UI build. A real Chromium smoke rendered
four exact frame/theme pairs once across three shards, matched the unsharded
capture, and preserved ordinary manifest replacement/retention/pruning.

The PR delivery record must include real CI wall time and the complete multiset
of frame/theme pairs compared with base `734485c8` (run `41814595510`), not only
summed capture counts. Both initial PR attempts also include newer master
`9d4bc717`: all 1,084 old pairs were retained exactly once, plus exactly 14 pairs
from its seven added frames. A fresh Chromium read of the exact synthetic merge's
registry independently confirmed all 549 frames / 1,098 declared pairs, matching
both runs with no duplicate, missing or errored capture. Browser results were
583 passed and 27 intentional skips; the newer base added five cases to the
original 578 passed / 27 skipped inventory.

Exact-head Codex review and passing full CI remain required before Merge-ready;
the startup-assumption failures and their test-only correction must remain
visible in that evidence, not be hidden behind a rerun.
