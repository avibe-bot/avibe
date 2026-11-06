# CLI runtime path and state architecture

**Status:** Phases 1–3 are landed. This document now records the measured result and the smallest remaining performance plan for a personal agent. It is a plan, not an implementation or deployment instruction.

## Result so far

The work targets the cost paid by every fresh `vibe` process and every stateful CLI call, not only Vault:

- **Phase 2 — readiness probe:** #3306 (`perf(storage): probe current SQLite state before repair`)
- **Phase 1 — lazy entry:** #3309 (`perf(cli): defer runtime imports until command execution`)
- **Phase 3 — read-only reads:** #3319 (`perf(cli): route current-state reads through SQLite read-only paths`)

On the hermetic 20-way mixed workload, the combined before/after result is:

| Metric | Before | After |
| --- | ---: | ---: |
| Per-command median | 16.6 s | **3.4 s** |
| Per-command p95 | 51.5 s | **11.9 s** |
| SQLite lock errors | — | **0** |

Phase 3 alone produced **no measurable speedup**. Its value is correctness and contention avoidance: read commands no longer need to acquire the bootstrap writer path. Do not attribute the full latency improvement to Phase 3.

The earlier 50-process instrumented Vault orchestration remains useful context: import-to-command p50/p95 was 29.389/48.001 s and bootstrap p50 was 22.465 s. Those numbers are an instrumented contention baseline, not a normal service-level latency claim.

## What is landed

1. **Readiness before repair.** An uncached initialization checks the exact Alembic head and existing data-completion evidence through a bounded read-only path. Unknown, old, missing, malformed, or incomplete state uses the existing repair path. No new readiness stamp or migration is required.
2. **Lazy CLI entry.** `version`, help, parse errors, and command admission avoid importing the selected command's heavy runtime until dispatch. Existing parser behavior and identity checks remain unchanged.
3. **Read-only state paths.** Current-state reads use explicit SQLite read-only connections/stores without bootstrap writes, model prefill, or index repair. Existing filtering and authorization behavior remains.

The readiness probe is an initialization optimization, not a schema lease. Process-lifetime memoization and the existing check-to-use window remain as they were; the probe does not add a pinned connection or post-check revalidation. A fresh/unpaired installation with no legacy deferred snapshots may remain on the conservative path when the existing bounded predicate cannot prove the migration is a no-op. It must not manufacture a marker merely to obtain the fast path.

## Remaining performance plan

A resident Avibe service already owns the runtime and is the place to remove repeated CLI process/bootstrap work. The next work is split by measured call frequency and effect:

| Phase | Scope | Delivery slice |
| --- | --- | --- |
| 4A | High-frequency Agent enqueue | **PR A #3327:** `agent run` through warm control IPC |
| 4B | Vault fetch process/bootstrap cost | **PR B:** Vault-fetch thin client over control IPC plus the Show server adaptation |
| 4C | Other high-frequency mutations | **PR C:** Watch, Task, and Session operations through the resident service |

The service owns the existing durable operation and warm state engine; it is not a second workspace proxy. Keep transactions short and blocking work off the controller loop. Measure each slice before expanding its command family.

### Service-unavailable behavior

When the resident service is not running or cannot accept a service-owned mutation, the CLI reports **`not executed`**. It does not silently fall back to in-process SQLite, start a hidden service, or claim success. This is the required user-visible behavior for the remaining mutation phases.

### Identity and Vault boundaries

- Requests carry the same invocation context the CLI already receives through injected `AVIBE_*` variables, and the service applies the same checks the CLI applies today.
- This is **parity, not a security boundary**: Agent shells run as the owner's OS user and can drop the environment or read `~/.avibe` directly. Do not add provenance hardening, enterprise roles, Organization/member models, or an identity matrix.
- Vault keeps today's custody, grant, check, audit, and no-replay semantics. The performance change is transport: a thin client and resident service/Show adapter, with avault remaining the custody boundary.
- There is no audit ledger, operation-ID migration, or other new Vault schema in this plan. No new database migration is planned for the remaining phases.

## Acceptance benchmark

Use a hermetic synthetic `HOME`/`AVIBE_HOME` with test-owned state and loopback stubs. Never benchmark by writing the owner's live `~/.avibe`, restarting the live service, or using production credentials.

- 200 mixed Agent-style CLI calls at 20-way concurrency, weighted toward `agent run`, `data query`, `harness status`, Watch/Task/Session commands, list reads, skill loads, and Vault fetch.
- A fixed-rate transcript writer using the real SQLite transcript path runs concurrently.
- Report whole-process, import, readiness/bootstrap, IPC, handler, SQLite wait/commit, and external-egress p50/p95/max separately; do not subtract percentiles.
- Require zero surfaced SQLite lock/timeout errors, no lost or duplicate transcript rows, visible retry exhaustion, and no controller-loop blocking from synchronous migration/SQLite work.
- Compare service warm/cold behavior and count child processes, writer occupancy, queue depth, retries, and host load.
- When the service is unavailable, assert `not executed` and prove there was no direct-write fallback or automatic service start.
- For Vault, count external handoffs and assert that an uncertain external result is never replayed. Preserve existing grants, checks, audit behavior, process boundaries, and exit codes.

Absolute thresholds should be set from the first post-phase measurement. The current relative targets are: version/help near interpreter-plus-parser cost, current-state reads with zero writer occupancy, warm mutation p95 independent of database history size, and transcript p95 no worse than the RC12 baseline under the same offered workload.

## Non-goals and limits

- No enterprise, Organization, member, role, or new identity design; personal-agent scope only.
- No security-boundary redesign: existing identity checks remain compatibility behavior and are not treated as authoritative against same-user Agent shells.
- No Vault audit ledger, operation-ID contract, readiness stamp, or migration.
- **Phase 6 (`vault run`) is dropped.** Do not expand this plan into a new child-process/custody protocol.
- No broad global writer, parallel SQLite-writer claim, arbitrary-SQL controller endpoint, or workspace proxy.
- AI Relay automatic refresh remains normal product behavior; pausing a panel is only an incident mitigation, never the architectural fix or acceptance configuration.

After PRs A–C, remeasure the complete mixed workload and decide whether any additional command family has enough measured benefit to justify another narrow phase.
