# Issue 1574: Memory Provider Root Ownership

## Background

A released Memory sidecar/rebuild path can create `memory/everos-root` and write
Avibe-generated control files without first proving the root is owned by the
current Memory store. Clear correctly refuses to delete a root without
`.avibe-memory-root.json`, so a failed durable Clear intent can remain open and
fence every later Memory operation.

## Goal

1. No sidecar or rebuild launch may create or write the provider root unless a
   current `ProviderRoot` ownership guard succeeds.
2. Durable Clear recovery must repair the narrow released first-start shape
   created by Avibe itself, then finish idempotently.
3. Unknown or unsafe unsentinelled roots must remain fail-closed.

## Solution

- Add a public `ProviderRoot.require_owned()` guard and pass it through the
  runtime, sidecar lifecycle, and rebuild process. Run it under the provider
  root process lock before directory preparation, generated root config writes,
  or subprocess spawn.
- Stop process directory preparation from creating the provider root. It may
  create only Avibe child-home/generated directories; the root must already be
  a private directory claimed by `ProviderRoot`.
- Add a Clear-only `ProviderRoot.recreate_empty_for_clear()` recovery path. An
  unsentinelled root is recoverable only when its top-level shape is the pinned
  EverOS first-start shape and both root TOMLs exactly match the private copies
  in `memory/generated`. The method writes and verifies the sentinel before
  reusing the existing destructive primitive.
- Keep ordinary `ProviderRoot.ensure()` strict so normal startup never adopts a
  non-empty root.

## Evidence

- Unit: sidecar and rebuild refuse an unclaimed root without writing or spawn;
  unsafe/mismatched recovery evidence remains rejected.
- Contract: released first-start root recovery is crash-idempotent through the
  existing sentinel and `recreate_empty()` sequence.
- Scenario: `MEMORY-CLEAR-203` retries a failed durable Clear marker against the
  released unsentinelled root shape, releases the fence, and allows normal
  reconciliation to continue.
- Residual manual: Incus Memory enable -> Clear -> restart remains part of the
  integration regression pass.

## Todo

- [x] Add failing ownership and Clear-recovery tests.
- [x] Enforce the provider-root guard in sidecar/rebuild launch paths.
- [x] Add narrow released-shape recovery to durable Clear.
- [x] Update scenario catalog and dependency observation.
- [x] Run focused Memory tests and Ruff.
- [ ] Complete PR review and CI gates.
