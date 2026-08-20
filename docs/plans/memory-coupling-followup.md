# Memory coupling follow-up audit

## Background

PRs #1582, #1588, #1589, and #1590 removed four concrete ways in which
Memory could block session archive, overwrite transport identity, leak its
metadata vocabulary into the durable queue, or fence dispatch and `/new`.
Those fixes establish a broader invariant: failure or latency in optional
Memory work must not fail, delay without a product-owned bound, or mutate a
non-Memory workflow.

## Goal

Scan every Memory touchpoint outside `core/memory/` and close remaining
violations of that invariant. Keep Memory-owned operations fail-closed where
required for authorization or data integrity, while keeping messaging,
session lifecycle, startup, and shutdown independent.

## Approach

1. Inventory imports, awaits, locks, callbacks, shared identities, and mutable
   state crossing the Memory boundary.
2. Add fast hermetic tests for each credible coupling path before changing it.
3. Fix the highest shared layer and keep provider/platform policy out of core
   delivery code.
4. Run focused tests, the Memory independence scenario guardrails, Ruff, and
   the broader affected suites.

## Findings and resolution

- Metadata leaf imports previously executed eager `core.memory` package exports
  and loaded the full store/worker/provider stack. Public exports are now lazy,
  and subprocess contracts prove storage sees only the metadata leaf.
- IM attachment filtering previously reached back through `core.handlers`,
  creating a handler/session-turn import cycle. The opaque lease and descriptor
  validation primitives now live in a dependency-free core leaf.
- Session capture locks, operation locks, and generation integers retained one
  entry for every historical session. A weak lifecycle state now stays alive
  only while an operation or capture snapshot can still use it.
- Synchronous text-capture setup exceptions could escape into message dispatch.
  The scheduling boundary now logs and drops optional work.
- Archived Workbench sessions retained process-local Memory authorization, and
  archive flush tasks could outlive Memory runtime shutdown. Archive completion
  now forgets the cache, and shutdown closes registration then settles tracked
  flush tasks before closing the runtime.

## Residual boundaries

- A user-initiated factory reset remains settlement-first during shutdown. Its
  durable recovery intent and provider-root ownership make abandoning it less
  safe than waiting; this is explicit Memory maintenance, not an ordinary
  messaging dependency.
- No live provider or IM regression is required for these process-local
  contracts. Existing five-platform behavior remains covered by unchanged
  admission and adapter tests.

## Todo

- [x] Audit controller construction, startup reconciliation, and shutdown.
- [x] Audit capture scheduling, cancellation, and lifecycle generations.
- [x] Audit `/new`, archive, and multi-scope lifecycle paths.
- [x] Audit routing/admission identity and leaf-module dependency direction.
- [x] Add regression coverage and fix confirmed violations.
- [x] Record residual risks and verification evidence.
