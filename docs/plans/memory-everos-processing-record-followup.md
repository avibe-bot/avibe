# Deferred EverOS Processing Record simplification

> Status: deferred decision note
>
> Revisit only after PR #1651 and its implementation are complete. This document
> authorizes no change to #1651 or to current product code.

## Decision

EverOS remains a separately supervised process. Avibe owns installation, startup,
health checks, wakeup, restart, and repair; Memory capture remains best effort and
may lose data.

After the best-effort writer ships, keep one user-facing **Processing Record** and
remove **Provider Call Log**.

Processing Record reads authorized EverOS-native data and shows:

- bounded original message text, not only a preview;
- run/strategy steps, status, timestamps, and errors; and
- generated Episode, Fact, and Profile content and identifiers.

Avibe does not duplicate that semantic history in its own durable observer store.

Provider Call Log removal includes its recorder, database, provider instrumentation,
raw request/response bodies, model/token/timing metadata, correlations, and unlinked
call UI/API.

## Contract

- Processing history may be incomplete or absent after loss, retention, migration,
  runtime replacement, or EverOS failure.
- Missing data is `unavailable`, not a complete empty result.
- Reads remain scope-authorized, bounded, and fail closed.
- Diagnostic failure never blocks capture or an EverOS call.
- Clear still deletes legacy call-log data, but new runtime code creates none.
- This adds no replay, retry, correlation, gap, anomaly, or exactly-once state.

## Sequencing

1. Finish #1651 and its accepted-loss writer implementation.
2. Re-audit the resulting EverOS data sources, Processing Record UI/API, and code
   ownership; verify that message text and semantic results are available directly.
3. Replace this estimate with measured file and test counts.
4. Implement the removal in a separate PR without changing capture reliability.

## Expected reduction

The pre-implementation estimate is another 3,000-4,500 production lines beyond the
#1651 simplification, or roughly 11,000-14,000 lines combined from the original
Memory design. Re-measure after implementation; this estimate is not a target or an
acceptance gate.

## Acceptance

- A user can inspect what was said, how EverOS processed it, and what semantic
  result EverOS produced when that native data still exists.
- No UI or API exposes provider HTTP audit details.
- Avibe has no durable per-call observer state.
- EverOS supervision and security boundaries remain intact.
