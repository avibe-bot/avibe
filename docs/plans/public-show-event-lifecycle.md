# Public Show event lifecycle

## Contract

A public event subscription remains usable only while its original page exists,
is online and public, and retains the admitted share ID. Check that binding at
the existing stream authorization checkpoints and before each replayed event.
On a terminal state, use the existing authorization frame and generator cleanup.
An idle subscription uses the existing 15-second heartbeat bound.

Private and authenticated stream authorization, projection, sharing controls,
and persistence formats remain unchanged. The stream reads committed state
because controller and CLI writers do not share the UI process's broker.

## State owners

- `ShowPageStore.apply_access` owns audience and link changes; legacy visibility,
  link rotation and custom link operations delegate to it.
- `ShowPageStore.set_offline` owns availability. Session archive also marks its
  page offline in the session transaction.
- A missing page fails the same binding check. Instance shutdown uses the
  existing broker close path; there is no separate instance-wide Show share flag.

## Verification and transport scope

Use isolated stores to cover replay, live delivery, idle expiry, HTTP response
completion, and subscriber cleanup. Keep existing private-stream and public
projection tests passing. Record focused regression, lint and type-check results
in the PR.

PR #4371 was still open at the starting master `0a64dbbc87`; this change covers
SSE. Its WebSocket transport must exercise this same generator lifecycle after
#4371 lands, with transport-specific closure coverage.
