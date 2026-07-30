# Harness Callback Ownership and Separation

## Background

Issues #918 and #922, fixed by PRs #919 and #923, established two contracts:

1. one terminal callback per asynchronous Agent Run; and
2. terminal callback text comes from the turn-final assistant result, never an
   Activity label or tool description.

Two incidents on 2026-07-30 exposed one remaining ownership error. A target
turn explicitly queued a separate Agent Run back to its caller while finishing
with an intentional silent terminal. The callback drainer treated that queued
child as if it were the parent's already-delivered callback.

## Live Evidence

The affected Sessions are:

- PM `sese8exsy8pnn`, Claude native Session
  `c9dca24f-14ed-41f6-83cb-dea22afb98f3`
- lane `sesggz7s4xuhz`, Codex native Session
  `019fadff-b0c7-76a3-b84d-04d9c224053e`

### Incident A

- Parent Run `c18fd5efe6e0` targeted the PM, finished `succeeded` with
  `result_text=""`, and recorded silent terminal Message
  `msg_00657ca71f29f90d085879c`.
- Intermediate PM Message `msg_00657ca710f9b286589d6f0` remained correctly
  stored as `author=agent`, `type=assistant`, `source=agent`.
- The PM explicitly queued full ruling Run `8b3237fa20ed` to the lane. It had
  `started_at=NULL`, but the parent was marked `callback_status=sent` with
  `callback_run_id=8b3237fa20ed`.
- The lane did not receive that intermediate Message through callback
  delivery. Its native Codex transcript shows it querying the PM Session at
  `2026-07-30T02:04:41Z`, then querying the exact intermediate Message at
  `02:04:47Z`; only afterwards did it report a truncated ruling.

### Incident B

- Run `e78503ad0e6b` was created with `--no-callback`, so both
  `callback_session_id` and `callback_run_id` are null.
- Intermediate PM Message `msg_00657cbef19fcadf59c6692` remained in the PM
  Session, followed by silent terminal Message
  `msg_00657cbefb91a4fb73fc539`.
- Full directed ruling Run `fd73893b6c41` remained queued with
  `started_at=NULL`.
- The lane's native transcript shows direct `vibe data query` calls at
  `2026-07-30T03:49:32Z` and `03:49:36Z` reading the PM Session and the exact
  intermediate row before it described the ruling as truncated.

The incidents therefore do not prove intermediate callback delivery. They do
prove that the callback ledger falsely identifies a separate queued child as a
sent callback, which makes later inspection and automation unable to distinguish
callback ownership from an independent directed Run.

## Root Cause

`ScheduledTaskService._enqueue_callback_run()` calls
`find_explicit_session_delivery()` before building the terminal callback. Any
agent-authored child Run with the parent id and callback target Session is
returned as the parent's callback, regardless of whether it has started,
completed, or was intended as a separate message.

The callback drainer then writes that child's id into the parent
`callback_run_id` and marks the callback `sent`. This conflates two durable
objects and lets an unstarted queue entry satisfy callback delivery.

## Required Behavior

- Only a callback Run created from the parent's turn-final `result_text` may
  satisfy the parent's callback policy.
- A successful empty terminal result produces no visible callback Run. Earlier
  assistant progress remains in the target Session only.
- An explicitly directed Agent Run remains independent, retaining its own id,
  source, parent lineage, queue position, callback policy, and exact message.
- A busy callback target may queue the terminal callback, but its body and
  provenance must still come only from the parent terminal result.
- No Harness-generated content is persisted as human-authored input, and no
  duplicate callback is created.

## Implementation

1. Remove explicit child-delivery substitution from the shared callback
   drainer and its store API.
2. Keep the change forward-only. Historical `callback_status=sent` rows remain
   inert and are never reset, replayed, or used to create a new callback Run.
3. Keep the existing terminal-result selection and exactly-once callback
   identity from #919 and #923.
4. Add consuming SQLite-backed tests through the real message dispatcher and
   callback drainer for:
   - intermediate preamble, tool boundary, intentional silent terminal;
   - intermediate preamble followed by a full terminal result;
   - separately directed full message while the callback target is busy; and
   - callback idempotency and non-user Harness identity.
5. Allocate `MESSAGE-DELIVERY-008` as contract-level partial coverage. PR
   #1104 currently owns `MESSAGE-DELIVERY-007`; `HFR-430` is already allocated
   on master.

## Historical Compatibility

The incidents predate this forward contract, but their stored delivery state is
not replayed. An old parent row already marked `callback_status=sent` remains
sent across upgrade and startup, even when its `callback_run_id` names a
directed `source_kind=agent` child. No migration re-arms it and no startup path
creates a replacement callback from its historical terminal text.

Read-only consumers distinguish the two child types from persisted
`source_kind`: `callback` identifies an automatic callback Run, while `agent`
retains directed-Run provenance and graph lineage. This classification never
mutates historical rows and never causes delivery.

## Scope

This change does not alter queue supersede, cancellation recovery, #1098,
#1072, authentication, authorization, ACL behavior, #1074, or the Markdown
silent-literal parser owned by PR #1104.
