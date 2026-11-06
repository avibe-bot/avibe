# CLI read-only paths (runtime-path Phase 3)

## Change contract

After shared `ensure_sqlite_state()` has proved the current state (or performed
its existing exceptional repair), ordinary direct CLI reads open SQLite with
`mode=ro` and `query_only=ON`. They do not acquire a writable engine, prefill Agent
models, or check/repair background indexes. Store read-only modes open existing
state only, like the existing `ShowPageStore(read_only=True)`; CLI helpers own the
shared readiness precondition rather than teaching every read constructor to
write.

Missing/old/unknown default state still follows probe -> existing repair before
the read. This is not an absolute no-write promise during initialization. Explicit
read-only database paths do not bootstrap. No new readiness stamp, schema revision,
needs-initialization refusal, identity model, lease, or service IPC is introduced.
The Phase 2 process memo and check-to-use window stay unchanged.

## Scope and ownership

- Extend the existing SQLite engine read-only option with query-only enforcement.
- Add explicit read-only construction to the existing Agent/background/run/Show
  event owners, retaining their queries, filtering, projections and writer paths.
- Route Agent discovery, Task/Watch projections, Runs/Harness reads, Session reads,
  Vault metadata reads, Show list/status/marks, and non-mutating data diagnostics
  through that capability. Keep guarded arbitrary SQL views/authorizer unchanged.
- Preserve invocation/admission checks. Their binding-state lookup remains the
  same read and legacy missing-state result, without creating a writable engine
  merely to inspect it. The CLI queue-list target resolver opts into an explicit
  read-only connection; the shared resolver keeps its default writer-compatible
  behavior for service and mutation callers. Pure-SELECT CLI preflight checks
  also use fresh read-only engines after readiness, without changing their
  authorization or routing decisions.
- Writes, enqueue, delivery/audit, approvals, model prefill by service/writer
  construction, and explicit repair retain current behavior. Bot list/show and
  Show path have existing ensure/create semantics and are not silently recast as
  pure reads. Raw MCP/activation precedence and lazy static entry remain intact.
- Preserve legacy file-backed store behavior; the new SQLite read-only capability
  does not redesign the file backend. Read-only constructors skip
  `ensure_background_indexes`; a head-revision database missing a managed index
  is repaired by the next writable store or service startup, not by a routine
  read. Legacy Agent rows with no model remain `null` until a writer-owned
  prefill. Runs/Harness reads retain their private-metadata policy; definition
  projections retain their existing redaction.

## Acceptance and tests

The credible regression is a read handler or transitive constructor acquiring a
writer engine or invoking prefill/index repair even when receipts are current.
Existing result-only tests can pass despite those side effects. Protect the real
owning boundary with current-state fixtures, actual query/output checks and a
write-attempt recorder that still fails when a handler catches a refusal. Verify
read-only engine enforcement, no Agent-model mutation, no index repair, unchanged
filter/projection output and continued writer behavior. Preserve fresh/old-state
repair behavior and existing admission/guarded-query coverage.

Measure exact base/candidate with all earlier phases on both sides. The benchmark
owner uses synthetic HOME/AVIBE_HOME/XDG, cold commands, 200 weighted calls at
concurrency cap 20 and real transcript writes at 20 Hz, with explicit stub and
integrity/rollback accounting. The agreed numerical phase gate is original
append roundtrip p95 <10 ms, zero lock errors and complete reconciliation; report
maxima and any regressions, not only the gate. Start only when one-minute uptime
load is below 10 and a coordinated reservation excludes other lanes' Playwright.
Record changing load and do not claim clean causality if it becomes confounded.
Full warm-service/Relay acceptance is not implied. Remeasure the accumulated
Phases 2/1/3 result before any later service-phase decision.

## Rollback and delivery

Revert this code-only change to restore write-capable read construction; no data
format or migration downgrade is required. Current-head review, CI and zero
unresolved threads are delivery gates. Report Merge-ready to the coordinating
Session, without merging, deploying, restarting or changing owner state.
