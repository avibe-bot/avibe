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
- The existing same-origin annotation `postMessage` boundary carries lifecycle
  commands and results. No server API or protocol changes are required.

## Contract

The iframe sends `query`, `start`, `stop`, `retry`, and `abort` requests with one
bounded request id and the existing cleanup context. The owning Chat frame sends
availability, started, preview, result, and typed error events. Both sides require
the expected origin and iframe window.

## Verification

- Show Runtime unit tests cover bridge validation, lifecycle, retry, punctuation
  insertion, and screenshot-comment field identity.
- Avibe UI tests cover host lifecycle and prove repeated internal minute segments
  do not end a recording.
- Existing Chat voice tests remain unchanged and continue to cover the shared
  recording/transcription implementation.
