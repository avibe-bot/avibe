# Browser and design CI parallelism

## Change contract

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
| 2 | five workbench-general specs, built composer-mention-picker, project-order | 8:08 |
| 3 | three onboarding-fidelity specs, home-media, pdf-preview | 8:10 |
| 4 | vault-integrity, workbench geometry, badge-triggers, chat-selection, voice-shortcut | 7:52 |

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

## Delivery status

Local validation passed: 63 CI-pipeline tests, 56 docs-classification/guidance
tests, 22 focused Vitest tests (selection and the existing suite runner),
changed-file Ruff and the production UI build. A real Chromium smoke rendered
four exact frame/theme pairs once across three shards, matched the unsharded
capture, and preserved ordinary manifest replacement/retention/pruning.

The PR delivery record must include real CI wall time and the complete multiset
of frame/theme pairs compared with base `734485c8` (run `41814595510`), not only
summed capture counts. Exact-head Codex review and passing full CI remain
required before Merge-ready.
