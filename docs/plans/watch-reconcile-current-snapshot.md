# Watch reconciliation against current snapshots

Issue: #1995.

## Change contract

- An enabled, eligible Watch must reach scheduling regardless of which reader
  refreshes the shared store first. No resume, unrelated write, or restart is
  required.
- A change arriving between scan and apply must converge on the next scan.
- Repeated unchanged snapshots must keep the same active worker, avoid idle
  runtime-state writes, and retain recovery, fuse, service-lease, and guarded
  lifecycle-write protections.
- Apply the same contract to the runtime-work lane and legacy polling loop.

## Approach

Always reconcile a successfully read snapshot through the existing idempotent
`reconcile_watches`. Store reload detection only controls cache freshness; it
cannot acknowledge scheduling work. Remove the redundant reconciliation dirty
flag instead of introducing another revision counter or notification path.
The runtime-work scan already copies every Watch; the legacy loop now checks
its in-memory list each tick. Unchanged reconciliation performs no durable write.
Already-cancelling workers retain their original cancellation so later scans
cannot interrupt their asynchronous teardown.

## Evidence and boundaries

HFR-485 covers file-backed and isolated SQLite-backed stores, reader-before-scan,
reader-between-scan-and-apply, legacy polling, retained worker identity, and
disable-after-reload. It uses real stores, scheduler handlers, and reconciliation,
with blocking coroutine workers instead of external waiter processes.
Existing HFR-179 tests cover single-flight store access and deferred wakes.

The prior tests checked reload and reconciliation independently, not whether a
different reader could consume the reload result before reconciliation.
No schema, CLI timeout, external notification, or deployment change is needed.
Live IM delivery and deployed-service verification remain separate acceptance work.
