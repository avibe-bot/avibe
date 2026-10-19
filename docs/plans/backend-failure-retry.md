# Retry a failed conversation turn

## Contract

The retry button is a user action on one authoritative backend-failure notice,
not a new backend retry protocol. Vibey's bounded automatic continuation is an
adapter-local policy over the same committed transcript.

- Only an attached `notify` with `event=backend_failure`, a `failure_id`, and a
  durable `turn_id` can offer the action. Ordinary notifications, detached
  completions, user stops, and authentication-recovery cards are unchanged.
- A service restart does not end an accepted Vibey Turn: startup recovery
  reruns that same Turn in place, as the transcript resume this action uses,
  with no Delivery and no notice (`vibey-restart-auto-resume.md`). Only a Vibey
  Turn that no longer qualifies takes the notice below.
- A service restart that terminalizes an accepted, non-restorable conversation
  Turn emits the same failure-notice contract, with the original Turn ID and
  backend retained across transport readiness waits and failed-send retries.
  Its visible restart explanation is unchanged. The notice does not settle
  another Turn or replay the original request; clicking continues through the
  existing admission path. Harness-owned interruptions retain their existing
  notice owner: its owed-notice drain carries the authoritative notice `turn_id`
  when delivering to the Run's own explicit Session. Generic Run failures
  without a Turn binding and fallback delivery to another Session remain
  informational; a Turn is never guessed from a failure ID or notification text.
  The existing admission checks still reject stale, busy, stopped, or
  acceptance-unknown work. Clicking continues that Session, not the original
  Task/Watch command or definition. A restorable live backend does not acquire
  a retry action.
  Recovery sets `metadata.replayed=true`: the notice describes an already-ended
  Turn, even on immediate delivery. This is not detached completion and does not
  disable Retry. The Web classifier refreshes durable Activity without settling
  its live generation or hiding a newer Turn's thinking indicator, on live
  delivery and on reload. The Python Activity classifier and fork progress
  filter apply the same nonterminal provenance, so history readback cannot
  reinterpret the notice as newer work ending. Ordinary live failures keep
  their terminal UI role.
  The Harness replay emitter applies the same historical provenance to every
  owed notice without adding a live Turn token to its delivery context.
- The action targets the notice's exact Session and failed Turn. The server
  rechecks access, archive/read-only status, current Turn, and pending input.
  A newer task, another live owner, or an unresolved native start blocks it.
- A normal failed Turn continues through the existing P3 Delivery admission
  with the literal backend input `continue`. Vibey is the deliberate exception:
  an accepted Vibey Turn retries from its committed transcript through a hidden
  `agent_initiated` Delivery, so no visible `continue` Message is created. It
  also performs up to five additional transcript continuations automatically
  for transient provider failures before emitting the failure notice. The hidden
  ownership row is excluded from Vibey's T3 unconsumed-input recovery, and a
  backend change before admission retires the Vibey-specific Delivery rather
  than sending an empty prompt to a native backend. Neither path rolls back
  transcript history or replays a committed tool call.
- A proven `not_written` Turn reuses its retained, unaccepted initial Delivery
  and the normal queue/admission path, preserving its prompt and attachments.
  Unknown native acceptance is not proof that replay is safe.
- Duplicate clicks use the same durable Delivery identity. Frontend disabling
  is feedback, not the concurrency guard. Admission rechecks the failed-Turn
  boundary under the existing writer transaction before starting work.
- Retry is independent of the composer draft. The original error remains
  visible; the button reports that retry was requested. Accepted continuation
  input remains in the ordinary transcript for truthful history.
- There is no backend-wide automatic retry loop, implicit model switch, auth reset,
  service restart, native history truncation, or new storage table. Vibey's bounded
  continuation is the adapter-local exception described above.

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
by the existing Turn state machine. For Vibey, the continuation marker is kept
in the hidden Delivery snapshot and the Agent loop resumes without consuming a
new user input row.

A retained original may already be queued before the click. Its action projects
`reserved` until the controller records this action's P3 admission or claims a
native start, so an unreachable controller does not lock an unsubmitted retry.

The `message.updated` Web event updates an existing row without replaying a
terminal event or increasing unread/activity counts. Reload and historical
message reads derive the same action state from durable storage.

The Web notification renderer uses the existing design-system Button with
localized labels (`重试` / `Retry`, `已请求重试` / `Retry requested`). It exposes
no action on read-only Sessions and cannot invoke a retry twice while submitting.
This first change targets the external Web conversation surface. Native backend
adapters retain the shared admission path and literal `continue` behavior;
Vibey additionally uses its loop resume path for accepted retries and automatic
transient-failure continuations. IM-specific notification button rendering is
not added here.

## Validation

- Shared contract tests: each backend, terminal versus ordinary/detached notices,
  cross-Session targets, stale Turns, busy Sessions, queued input, and unknown
  native acceptance.
- Real storage/admission tests: same-notice concurrency and duplicate delivery,
  pre-write prompt/attachment preservation, failed submission recovery, and
  composer draft preservation.
- Agent-core and Vibey adapter tests: transcript-only resume, bounded automatic
  continuation, hidden Vibey Retry input, and committed tool-result reuse.
- UI tests: eligible notification, ineligible/read-only rows, pending/accepted
  feedback, failure recovery, and draft-independent action; mounted ChatPage
  preservation of newer Activity/thinking after replayed notices, with ordinary
  terminal controls; production UI build.
- Isolated local Incus verification only; never the running workstation service
  or a remote tenant. No real upstream model request is needed for fault cases.

Scenario contracts: `MESSAGE-DELIVERY-026` through `MESSAGE-DELIVERY-028`.
Restart notification binding and continuation: `MESSAGE-DELIVERY-030`.
Harness-owned notification binding and continuation: `MESSAGE-DELIVERY-320`.
Vibey restart rerun without a notice: `MESSAGE-DELIVERY-327` through
`MESSAGE-DELIVERY-333`.

## Review-boundary correction contract

The second findings-bearing head exposed the same ownership class in new paths:
resume is control-plane work, not another model input. The orchestrator stops
path-by-path patching and retains the existing Delivery/Turn/Agent model, with
these complete boundary fixes (no new retry service or state machine):

- Internal dispatch derives resume permission only from the durable Delivery's
  `type`, `author`, `source`, and server marker. Admission, FIFO claim, and final
  dispatch must reject that control Delivery on any resolved non-Vibey backend.
- Provider errors distinguish replay permission (`retryable`) from transcript
  continuation permission (`continuable`, default false). Transient streamed
  failures can allow continuation without request replay; Model Hub recovery
  exhaustion, explicit refusals, permanent errors, and local errors allow neither.
  AgentError preserves these permissions and Retry-After. Automatic continuation
  honors the delay and one bounded elapsed budget across all five continuations.
- The adapter supplies the active durable Turn ID to transcript writes, separately
  from input consumption. T2 recovery keeps the originating failed call's Turn;
  retry execution writes new events against the retry Turn.
- Resume pins include every accepted input of the failed task, including returned
  steers and inputs inherited through retry ancestry. Automatic resume unions
  newly admitted sequence numbers with the same Agent's current pins.
- Hidden control rows stay out of model context and out of the indexed population
  of recoverable inputs. Recovery must not resolve attachments or fetch Delivery
  snapshots for historical hidden rows. Recovery seeks Messages first, then
  their Deliveries by indexed `message_id`, and Turns by primary key; tests
  reject historical scans at every join, not only the Message table. Migration
  upgrade, downgrade, and head-schema repair preserve those indexes.

Validation uses real Web-to-internal dispatch, FIFO backend changes, provider
classification through continuation, returned steers, repeated user retries,
event attribution, and recovery query plans. The existing CI failure in
`tests/test_vibey_agent_context.py` reproduces the lost provider permission and
must pass without weakening its source-failure attribution assertions.

## Known by design

Native backend APIs do not provide one verified retry operation for all terminal
failures. Claude's recovery of certain unfinished process-exit Turns is narrower
than this action and is not used as a general shortcut. Historical notices
without authoritative Turn linkage are not guessed into retry targets.
