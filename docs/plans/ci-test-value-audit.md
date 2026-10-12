# CI test-value audit and latency reduction

## Intent and acceptance

Owner request (2026-10-13): remove redundant, obsolete, tautological, or
implementation-pinning tests and optimize the valuable remainder. Aim for a
complete PR `lint` at or below the early-September reference (~9m21s), not a
faster check achieved by hiding failures. Report both elapsed workflow time
(including queue gaps) and execution critical path / total runner seconds.
Use complete successful exact-head runs; a single fast sample is not a stable
performance guarantee.

Base: `b85e3c567e598eb826c9774c545bd56aad4c3e47` (PR #2518 already merged).
Recent measurements: PR run 38258683681 = 18m33s, master run 38261750469 =
18m15s. Python files run in separate processes; a stale timing snapshot and
repeated fixture work amplify a ~536-file suite. UI validation and remaining
browser suites currently execute serially in one job. Earlier Model Hub UI
regression now runs daily/manual; Windows platform suites run on master, with
Windows latest installer smoke retained for PRs.

## Test-value rule

For each deletion record the exact test/file, the asserted contract, why the
assertion has no independent value, and any remaining owning-boundary coverage.
Credible candidates are tests of source spelling/internal layout, duplicate
regressions for an already-covered failure class, tests whose mocks implement
what they assert, dead-feature requirements, and exhaustive matrices with no
additional boundary risk. Age, passing history, AI authorship, runtime, or test
count alone are not a deletion reason. No deletion quota.

Keep unique security/authorization, data preservation, migration/compatibility,
installer, cancellation/failure propagation, and real runtime boundary coverage.
Do not add test-only production APIs, relax assertions, catch failures, add
retries, or mark valuable checks optional just to meet the latency target.
Optimize expensive fixture preparation without sharing mutable test state;
keep migration tests genuinely unseeded and hermetic filesystem/process access.

## Integration contracts and ownership

One integration PR, owned/reviewed by session `sesp2bd67rp9c`; no lane opens a
stacked PR or merges. Internal lane commits are integrated and independently
reviewed by the orchestrator before the single PR is pushed.

- Orchestrator: `.github/workflows/`, `scripts/ci_*`, timing snapshot,
  `tests/test_ci_*.py`, plan/evidence integration, review/CI delivery.
  Retain existing required context names and fail-closed aggregates. Every
  retained Python file must be selected exactly once. Split independent UI
  tasks only with explicit required dependency wiring. Reuse existing tools.
- Python lane: `tests/` excluding `test_ci_*.py`, plus its own evidence file
  `docs/plans/ci-test-value-python.md`. Optimize measured slow test fixtures and
  remove proven low-value tests. No production behavior changes, workflow or
  shard changes; report cross-boundary opportunities rather than editing them.
- UI lane: `ui/` test files/tooling/config and its own evidence file
  `docs/plans/ci-test-value-ui.md`. No application behavior changes; preserve
  existing package script interface unless coordinated. No workflow edits.
- Independent audit lane: read-only across all tests. Identify concrete
  deletion/overconstraint candidates and challenge coverage assumptions;
  no code writes. Its report is evidence, not final review authority.

No API, wire format, persistent schema, allocated IDs, credential or deployment
changes. No remote environments or local Avibe restart. Keep the primary
checkout and unrelated active worktrees untouched.

## Active execution

- Integration branch: `vibe/20261013-ci-test-value`, orchestrator worktree
  `.worktrees/avibe/ci-test-value-audit`.
- Python implementation: session `sesm4rsqtgcq5`, branch
  `vibe/20261013-ci-value-python`.
- UI implementation: session `sesswafk5qzrr`, branch
  `vibe/20261013-ci-value-ui`.
- Independent read-only audit: session `sesvtw3epcdgv`, worktree
  `.worktrees/avibe/ci-test-value-independent`.
- Shared contract commit before dispatch: `e089ae4429aae72d49b448ca4cc59d806cc2f82e`.
- Orchestrator has removed shell/toolchain self-tests, split required UI lanes,
  and rebuilt a complete timing snapshot. See `ci-test-value-evidence.md`.
  Integration waits for lane commits; no partial PR has been pushed yet.

## Work sequence

1. Collect complete per-file timings from successful PR/master runs; preserve
   source SHAs/run IDs and inspect phase breakdown for actual work vs waits.
2. Parallel test-value audits and targeted fixture/tooling reductions; record
   deletion rationale and before/after focused measurements.
3. Integrate justified removals; refresh shard timings and evaluate balanced
   plans. Prefer reducing real work before increasing runner concurrency.
4. Parallelize the retained UI lanes if still on the critical path. Keep
   cancellation/skipped/failure dependencies fail-closed and names stable.
5. Focused validation + changed Python Ruff + UI build; push a non-draft PR,
   arm one durable review/CI Watch, and validate full current-head CI and Codex.
6. Compare actual CI against target. If unmet, diagnose the remaining measured
   tail and continue bounded, evidence-led changes rather than claiming success.

Merge requires fresh explicit owner authorization for this new PR; prior
permission applied to #2518 only. Final report includes deleted contracts,
retained coverage, actual time saved, runner cost, and remaining risks.
