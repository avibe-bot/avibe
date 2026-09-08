# Retry a failed conversation turn

## Contract

The retry button is a user action on one authoritative backend-failure notice,
not a new backend retry protocol or an automatic retry policy.

- Only an attached `notify` with `event=backend_failure`, a `failure_id`, and a
  durable `turn_id` can offer the action. Ordinary notifications, detached
  completions, user stops, and authentication-recovery cards are unchanged.
- The action targets the notice's exact Session and failed Turn. The server
  rechecks access, archive/read-only status, current Turn, and pending input.
  A newer task, another live owner, or an unresolved native start blocks it.
- A normal failed Turn continues through the existing P3 Delivery admission
  with the literal backend input `continue`. It does not roll back history,
  replay already attempted tools, or pretend to resume an arbitrary native Turn.
- A proven `not_written` Turn reuses its retained, unaccepted initial Delivery
  and the normal queue/admission path, preserving its prompt and attachments.
  Unknown native acceptance is not proof that replay is safe.
- Duplicate clicks use the same durable Delivery identity. Frontend disabling
  is feedback, not the concurrency guard. Admission rechecks the failed-Turn
  boundary under the existing writer transaction before starting work.
- Retry is independent of the composer draft. The original error remains
  visible; the button reports that retry was requested. Accepted continuation
  input remains in the ordinary transcript for truthful history.
- There is no implicit model switch, auth reset, service restart, automatic
  retry loop, native history truncation, or new storage table.

## Interfaces and ownership

`POST /api/sessions/{session_id}/messages` accepts a top-level `retry_for`
containing the failed notice Message ID. It derives the prompt, content,
metadata, and Delivery identity from server-owned state, never from a supplied
replacement prompt. Other message submissions keep their existing contract.

The shared failure-retry helper validates and reserves the action within the
caller's existing SQLite writer transaction. The notice records its Delivery
link; the existing Delivery/Turn state machine remains the execution owner.
The controller revalidates the action before admission to cover the interval
between Web reservation and native dispatch.

The only persisted notice field is `content.failure_retry.delivery_id`.
Read-side `failure_retry.state` comes from that Delivery, never a separately
maintained acceptance flag. A reserved action can wake the same Delivery again
after a lost response; an admitted action is idempotent. Definitively retired
unaccepted continuations can be reserved again only while the failed-Turn
boundary still holds. Server-owned `backend_failure_retry` Delivery history
binds the notice, source Turn, and exact retained batch for admission and queue
drain checks. Once a native start is claimed, subsequent recovery stays owned
by the existing Turn state machine.

A retained original may already be queued before the click. Its action projects
`reserved` until the controller records this action's P3 admission or claims a
native start, so an unreachable controller does not lock an unsubmitted retry.

The `message.updated` Web event updates an existing row without replaying a
terminal event or increasing unread/activity counts. Reload and historical
message reads derive the same action state from durable storage.

The Web notification renderer uses the existing design-system Button with
localized labels (`重试` / `Retry`, `已请求重试` / `Retry requested`). It exposes
no action on read-only Sessions and cannot invoke a retry twice while submitting.
This first change targets the external Web conversation surface. All three
backend adapters benefit from the shared admission path without separate retry
implementations; IM-specific notification button rendering is not added here.

## Validation

- Shared contract tests: each backend, terminal versus ordinary/detached notices,
  cross-Session targets, stale Turns, busy Sessions, queued input, and unknown
  native acceptance.
- Real storage/admission tests: same-notice concurrency and duplicate delivery,
  pre-write prompt/attachment preservation, failed submission recovery, and
  composer draft preservation.
- Web route tests: access checks, canonical server-generated payload, source
  notice update, and reloading persisted action state.
- UI tests: eligible notification, ineligible/read-only rows, pending/accepted
  feedback, failure recovery, and draft-independent action; production UI build.
- Isolated local Incus verification only; never the running workstation service
  or a remote tenant. No real upstream model request is needed for fault cases.

Scenario contracts: `MESSAGE-DELIVERY-026` through `MESSAGE-DELIVERY-028`.

## Known by design

Native backend APIs do not provide one verified retry operation for all terminal
failures. Claude's recovery of certain unfinished process-exit Turns is narrower
than this action and is not used as a general shortcut. Historical notices
without authoritative Turn linkage are not guessed into retry targets.
