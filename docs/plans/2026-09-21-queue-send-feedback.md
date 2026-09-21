# Queue send-now recovery and feedback

## Contract

- A Memory authority check that fails before a native write returns the exact
  claimed Delivery to the retryable FIFO queue. It must not replay old input,
  invoke Stop, or weaken the unknown-write fence.
- A send-now invocation owns its queue, error, working, and progress updates only
  until another invocation or session navigation invalidates it. Returning to
  the same session ID does not restore ownership. A later authoritative Turn
  event also prevents the request from undoing the current working state.
- Both rejected transport promises and non-success HTTP responses reconcile the
  authoritative queue before selecting failure feedback. Retry feedback requires
  the exact clicked Delivery to remain present and unfenced. An absent, fenced,
  unreadable, or superseded snapshot gets neutral, localized feedback.
- Raw controller/socket details remain diagnostics, never user-facing copy.
  No automatic retry or synthetic Message is introduced.

## Review circuit-breaker diagnosis

The complete GitHub inventory for PR #2086 was checked on 2026-09-21, including
resolved/outdated threads. Two distinct heads have findings:

| Root-cause class | `814261de3a` | `a4379a743b` |
| --- | --- | --- |
| Async request ownership | #4059515552: recheck after awaited refresh | #4059672447: invalidate across A → B → A |
| Retry feedback without delivery evidence | #4059515553: transport ambiguity | #4059672451: stale-head HTTP response |
| Localized failure boundary | #4059515557: raw controller detail | No new finding |

The first two classes repeat across two reviewed heads, so further patching was
paused for a full boundary review. The orchestrator inspected the previous fix,
its A → B consuming test, the session-reset effect, queue snapshot ordering,
`ApiContext.sendQueuedNow`, and the controller FIFO admission refusals.

Diagnosis: the first repair guarded post-refresh writes but never invalidated
the invocation on navigation. It also applied evidence-based feedback only to
thrown requests, although `handleError: false` returns HTTP failures as values.
Queue membership alone is insufficient when the same row is still held behind
a native-write fence.

Scope decision: retain the existing invocation generation and queue-read ordering.
Advance the invocation generation in the session-reset lifecycle and share one
failure-reconciliation path for HTTP and transport errors. Reuse the queue's
existing fenced-state predicate. No database, backend admission, API-shape, or
runtime deployment change is required for this review repair. The existing Turn
epoch protects working-state rollback while the queue refresh is pending.

## Validation and delivery

- Component tests exercise A → B and A → B → A during both send and refresh,
  preserve a newer invocation's spinner, and cover HTTP/transport failures with
  retryable, fenced, absent, and unreadable queue snapshots.
- Run the focused ChatPage suite, changed-file lint, UI build/test typechecks,
  and relevant hermetic browser queue tests.
- Retain the backend pre-write failure regression and existing FIFO/no-Stop
  behavior; do not infer deployed or live-model acceptance from hermetic tests.
- Keep the original PR Watch/cursor. Require automatic exact-head Codex review,
  complete current-head CI, and zero unresolved threads before readiness.
  Merge, installation, and regression updates remain separately authorized.
