# Show Session Event Pipeline Plan

## Summary

Show Runtime can now render interactive pages and the SDK can emit typed
session events. Vibe Remote already owns the durable event store, transcript
projection, private/public Show Page routing, and optional dispatch to the
active agent session.

This slice closes the browser usability gap: a private Show Page should be able
to use `@avibe/show-sdk` without manually hardcoding the session id, event
endpoint, stream endpoint, or write token.

## Current State

- `show_session_events` persists typed events and links transcript messages.
- Private `/show/<session-id>/__show/events` accepts authenticated event POSTs.
- Private and public Show Pages can list and stream events.
- `dispatch: true` human intent and annotation events reserve a pending
  transcript row, then enter the same `/internal/dispatch_async` turn path as
  Chat. `SessionTurnManager` starts or queues the turn; accepted prompts and
  replies surface through the session-scoped `message.new` stream, while queued
  prompts surface through `queue.updated`.
- The static Vibe Remote template reads the write token cookie, but managed
  runtime pages currently depend on their own generated config and can miss the
  token.

## Design

Vibe Remote remains the source of truth for session event semantics.

For private Show Page HTML responses, Vibe Remote injects a small
`globalThis.__AVIBE_SHOW__` bootstrap before the first module script:

```ts
globalThis.__AVIBE_SHOW__ = {
  sessionId,
  basePath,
  eventsPath: "__show/events",
  streamPath: "__show/events?stream=1",
  writeToken
}
```

The injection is intentionally limited:

- only private `/show/<session-id>/...` pages receive a write token
- public `/p/<share-id>/...` pages stay read-only
- API responses and non-HTML assets are not modified
- the runtime sidecar still never receives UI cookies or auth headers

The event POST endpoint also treats the URL session id as authoritative. If a
browser payload includes `sessionId` for a different session, Vibe Remote
rejects it instead of silently recording a cross-session event.

## Non-Goals

- No screenshot image upload storage in this slice.
- No new annotation toolbar or overlay UI in Vibe Remote.
- No public event submission.
- No extra agent dispatch policy beyond explicit `dispatch: true`.

## Validation

- API tests for private runtime config injection.
- API tests that runtime-side injected config preserves existing user config.
- API tests that public Show Pages do not receive write config.
- API tests for cross-session `sessionId` rejection.
- Existing event persistence, transcript, SSE, and dispatch tests.
