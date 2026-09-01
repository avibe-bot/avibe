# Show Page Annotation Voice Bridge

## Goal

Let annotation text fields inside an embedded Show Page use Avibe's existing
voice dictation experience without adding a recording-duration or aggregate-file
limit and without implementing a second transcription stack in Show Runtime.

## Ownership

- The Show Page iframe owns the microphone button, field state, realtime preview,
  and final text insertion.
- The Avibe client owns microphone capture, realtime transcription, internal WAV
  segmentation, HTTP fallback, retry, and cleanup through the same modules used
  by Chat.
- One client-wide capture claim prevents hidden Chat and Show Page recorders from
  running concurrently. The host transfers ownership synchronously when it
  accepts an explicit start, before asynchronous availability or capture setup.
  A newer explicit voice action finishes the previous capture, and a capture
  stopped during setup still completes its buffered-audio finalization.
- The existing same-origin annotation `postMessage` boundary carries lifecycle
  commands and results. No server API or backend behavior changes are required.

## Contract

The iframe sends `query`, `start`, `stop`, `retry`, and `abort` requests with one
bounded request id. Start and retry carry the cleanup context that matches the
current insertion snapshot. The owning Chat frame sends availability, started,
preview, result, and typed error events. Both sides require the expected origin
and iframe window.

An unanswered start handshake times out and aborts without imposing any limit on
recording duration. Reloading the iframe disposes its old voice host, and ASR
availability is shared only while a request is in flight so later requests see
configuration changes and transient recovery.

## Known By Design

Unlimited recordings retain minute-sized browser Blob segments in the
in-session retry batch until transcription succeeds or the user explicitly
discards it. This matches Chat and permits re-upload when the server rejects a
previous segment receipt. The bridge does not persist voice audio to browser
storage and does not add a duration or aggregate-size cap, so memory use grows
with the audio that remains eligible for retry.

## Verification

- Show Runtime unit tests cover bridge validation, lifecycle, retry, punctuation
  insertion, and screenshot-comment field identity.
- Avibe UI tests cover host lifecycle, availability refresh, retry context, and
  client-wide capture ownership, and prove repeated internal minute segments do
  not end a recording.
- Existing Chat voice behavior continues to use the shared recording and
  transcription implementation; only cross-surface capture ownership is added.
