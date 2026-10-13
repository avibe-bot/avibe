# CI under ten minutes: queue demand and measured execution

Owner request (2026-10-13): continue the CI optimization roadmap toward a
complete successful `lint` result under ten minutes, including runner queue
waits. Not permission to skip failures, publish/deploy, reset environments,
upgrade the GitHub plan, or merge without a new explicit owner instruction.

Base: master `a5c6cbf17` (#2542). Session `sesp2bd67rp9c` owns the task branch
`vibe/20261013-ci-under-10m`; no parallel mutable owners.

## Evidence and honest acceptance

- #2527 execution critical path reached ~9m21s but total elapsed was 15m43s.
  Organization Free concurrency is 20 hosted jobs. An org-wide timeline showed
  all 20 slots busy for 348 of one shard's 381 seconds of queue wait.
- After #2540/#2541, successful master run 38355727304 (9efb8b95c) has 540
  complete file records, 3943 launcher seconds (3745.60s before interpreter
  shutdown), and a 332–506s file-work imbalance across nine shards. The longest
  job is 551s. Rebalancing eight lanes may reduce queue demand without making
  execution exceed 600s; replay is evidence for a trial, not a guarantee.
- Latest master run 38358514819 is **failed**, not a fast success: ARM Agent
  setup-python's PowerShell archive extraction crashed with an internal CLR
  error before any tests ran. Preserve this observation; do not weaken that
  gate or mix partial failed-run data into the timing snapshot.
- Accept only complete successful exact-head runs. Report elapsed seconds,
  dependency critical path, ready-to-start queue gaps and total runner seconds.
  A single run under 600s is a measured sample, not a P95 guarantee. Saturated
  hosted capacity cannot be solved solely by YAML; surface capacity limits
  rather than repeatedly increasing shard count.

## First bounded change

1. Only test workflow `lint` uses latest-wins cancellation for its existing
   per-PR / master concurrency group. Every `push` trigger here is master;
   publishing/deployment workflows and other PRs' groups are untouched. A
   superseded commit may lack a completed master verdict; the newest must still
   pass all gates. **Master pushes always use full CI**, even for a docs-only
   before..after diff: a docs push may cancel an unvalidated earlier code push.
   Documentation fast-path authority is retained for PRs only. This deliberate
   trade-off was identified by the independent design reviewer and avoids
   creating a validation gap. No manual cancellation of peers' runs.
2. Refresh per-file launcher timings from complete successful current-generation
   CI. Replay a separate run to check balance before trialling **eight** lanes
   rather than nine. Keep all discovered files exactly once, isolated-process
   watchdogs, integration exclusions and required check names.
3. Add an **observational-only impact category** to the existing scope summary,
   reusing its complete validated Git diff. UI-local / backend-local categories
   are candidates for later analysis, not skip authority. Only the existing
   docs-only decision is exposed as a workflow output; unknown/shared/mixed
   changes stay full. No workflow condition consumes shadow telemetry.
4. Run focused validation and first full PR CI; observe current-head review/CI
   through one durable Watch. If the target is unmet, diagnose the actual tail
   before another change. No inferred win from cancellation or missing jobs.

## Later decisions, only after first run

- Observe a real docs-only run when naturally available; do not fabricate a
  stacked PR or introduce a force-skip input.
- Consider grouping short checks only if worth migrating required contexts;
  never silently delete the required checks or add placebo success wrappers.
- Consider safe fixture overhead or batch-runner experiments only with real
  order-independence and hermeticity proof. The current process-isolation
  workaround remains needed until then; no broad local full-suite gate.
- Keep Windows artifact/installer/engine coverage. Cache downloads rather than
  user state only if observed setup dominates after queue reduction.

## First candidate evidence

- Snapshot: median launcher elapsed time from complete successful master runs
  38350418272 (`f73106bd1`, 538 files, 3843s) and 38355727304 (`9efb8b95c`,
  540 files, 3943s); new files use their one available measured sample. The
  current network guard file has no sample in those runs and retains the
  existing structural fallback, not an invented measured duration.
- Eight-lane median planning predicts 487–488.5s of launcher work each.
  Replaying the later source sample gives 476–503s; using only the older sample
  to plan and holding out the newer gives 466–515s. Add roughly 30–45s measured
  setup/cleanup overhead: eight lanes are a plausible sub-ten-minute candidate
  with less queue demand, not a promised service-level bound.
- Observational labels are `docs-only`, `ui-source-only`,
  `python-source-only`, `shared-or-mixed`, `unknown`. They classify path areas,
  not independent behavioral boundaries: App/API/routing or migration code
  may still couple both sides. They appear only in stdout/Actions summary,
  never `GITHUB_OUTPUT`; the sole output remains `docs_only` and no new skip
  condition exists. Incomplete/invalid mode/event evidence yields unknown/full.
- Independent read-only design review on contract `c4c014684` found the
  docs-push cancellation hole above; no artifact consumer or isolation issue.
  All assertions, installers, branch protection and release workflows remain.

- Focused validation: 50 docs/event/impact cases and 62 pipeline/shard cases
  pass in separate invocations, including a real code-B/docs-C push whose
  effective output must be full and a matching docs PR whose output stays true.
  The first combined probe exceeded its command deadline during execution and
  is not counted as passing; the bounded focused reruns completed normally.
  Ruff 0.4.9 and actionlint pass. No repository-wide local gate was run.
- No ARM code change is needed for the observed failure: job 115166968293
  failed inside setup-python extracting the x64 archive with PowerShell
  `Internal CLR error (0x80131506)` / exit 3221225477, before dependency setup
  and tests. It is not counted as success; future full master CI must verify
  Windows Agent coverage again. No retries/failure suppression were added.

## Validation boundary

Existing pytest owns changed CI behavior. New cases must protect a credible
failure mode (accidental gating on observational classification, incorrect
path-side handling, losing required coverage or cross-PR cancellation), not
pin tool versions for their own sake. Existing root/mode/rename/event validation
from #2534 stays conservative. No runtime/application behavior changes.
