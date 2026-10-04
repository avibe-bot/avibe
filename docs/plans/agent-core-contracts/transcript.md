# C-5 Transcript rows

The Avibe Agent's model context is stored in the existing `messages` and `agent_events` tables; there is no other
copy (plan §5.1). Row payload shapes: [`transcript-rows.schema.json`](transcript-rows.schema.json).

## 1. Membership and order

A row belongs to a Session's model context exactly when its `context_seq` is non-null. `context_seq` is an integer,
unique per Session across both tables (partial unique index on each table, plus the single-writer rule below).

| Entry | Table | `type` / `event_type` | `visibility` | Payload |
| --- | --- | --- | --- | --- |
| user or steer input | `messages` | `user` | — | `content_json.model` = `ModelInput` |
| harness input | `messages` | `harness`, `agent_initiated`, `annotation` | — | `content_json.model` = `ModelInput` |
| model response | `messages` | `assistant` (not final), `result` or `error` (final) | — | `content_json.model` = `ModelResponse` |
| tool result | `agent_events` | `tool_result` | `context` | `content_json` = `ToolResult` |
| checkpoint | `agent_events` | `context_compaction` | `context` | `content_json` = `Compaction` |
| cleared result | `agent_events` | `context_edit` | `context` | `content_json` = `ContextEdit` |
| hook and guard state | `agent_events` | `agent_state` | `context` | `content_json` = `AgentState` |

A final response is `error` when it failed by itself (no text of its own: an empty answer, or a refusal or safety
stop without an explanation), as the other backends' failed terminal rows are. A successful final with nothing to show
(a silent reply) is the hidden response type `assistant`: context, but no transcript row, inbox reply, or unread
result, as the other backends persist nothing visible for it; a terminal write at delivery (a run that failed after
the commit) types it `error`. Context loading accepts all three.

Display-only rows keep `context_seq` null: `interim`, `notify`, an `error` that reports a run failure, `vault`,
`output`, queued or removed inputs, and the `tool_call` trace row written at tool start (its `metadata_json` carries
`tool_call_id` and `job_id` so the activity panel can pair it with the result). Audit rows are never context
either: `agent_events` with `visibility = 'audit'` and no `context_seq`, written by `append_audit`, as
`context_checkpoint_turn` (`CheckpointTurn`, C-9 `context.md` §6, with its own attempts' partials and usage) or
`model_attempt` (`ModelAttempt`, the usage of a conversation attempt that did not become a response, in every mode). The activity panel does not read them.

## 2. Writing

- Every store implements the whole `TranscriptStore` protocol (`harness/store.py`) with the same behavior: an append
  returns exactly the entry `load` reads back, `created_at` (epoch seconds) included; a response keeps C-9's request
  facts in `content_json.model.request`; a payload row needs a payload kind and version 1, and `append_payloads`
  commits all of its rows or none; consuming the same input again returns its entry, and a different message for it
  is refused; a call is settled once (a second result returns the first); audit rows never load. One contract suite,
  `tests/test_transcript_store_contract.py`, runs the same tests on the SQLite store, the adapter's store over it,
  and the in-memory store the engine tests use.
- The loop is the only writer of a Session's `context_seq`, under a per-Session lock held across the transaction:
  `next = max(context_seq of the Session in both tables, fork_source_context_seq if the Session is a fork) + 1`, so a
  fork's first entry follows its inherited prefix.
- Inputs already exist as rows when they are submitted. The loop sets `context_seq` and `content_json.model` on that
  row when it consumes the input. Each entry is its own transaction; an input accepted by `steer` but not yet
  consumed when a crash happens is admitted into the context by the adapter at resume (`recovery.md` T3).
- A response row is inserted at `message_end` with both `content_json.model` and its commit-time display
  `content_text`.
- A tool result row is inserted at `tool_finished`. The `tool_call` trace row is inserted at tool start without a
  `context_seq`.
- Each commit is one SQLite transaction. The adapter delivers a row to surfaces only after it commits, through the
  same emit path as the other backends, naming the row (`MessageOutput.persisted_row_id`). Every dispatcher persist
  site then writes that row's display columns instead of inserting a second row: `content_text`, the display keys
  of `content_json` (`text`, `kind`, `quick_replies`, `result_footer`, `citations`), the delivery target's
  `platform` and `scope_id`, `metadata_json` (including the `delivery_suppressed` marker and its promotion), the
  output's `native_message_id` unless another row holds it, and a final's `result` / `error` type. Nothing is
  replayed: a delivery a crash or send failure interrupts is lost, as for the other backends
  ([`recovery.md`](recovery.md) §Delivery).

## 3. Projection

`project(session_id, fork_point=None) -> list[Message]`, a pure function of the rows:

1. Collect the context rows: the Session's own, plus its fork ancestry (§4).
2. Order by `context_seq`.
3. If a `context_compaction` row exists, take the latest; the context becomes its checkpoint message (its `summary`,
   then each `state` text as its own block) followed by the rows with `context_seq >= first_kept_seq`, excluding every
   `context_compaction` row (including the selected one) and `agent_state` rows. A tool result whose call was
   summarized leaves with it.
4. Apply `context_edit` rows: the latest edit per target replaces that tool result's content with its placeholder;
   the `context_edit` rows themselves are then removed, so the result is only messages.
5. Answer any tool call that still has no committed result with the synthetic interrupted result from
   `cross-provider.md`. Projection never consults a live job; resume settles open calls durably first
   ([`recovery.md`](recovery.md)).
6. Prepend the rebuilt system prompt and hook-rehydrated messages; these are not rows. State rendered for a
   checkpoint is in its row, so projection stays a pure function of the rows (C-9 `context.md` §7).

## 4. Fork

The child Session's metadata already records its parent as top-level keys `fork_source_session_id` and
`fork_source_message_id` (written by `reserve_forked_session`, read by `fork_metadata_from_session_metadata` in
`core/services/session_fork.py`); C-5 reads those keys and adds no new fork shape.

- `anchor_seq` is resolved once, when the fork is reserved, as the largest `context_seq` in the source Session among
  rows at or before the anchor message, and persisted as the top-level metadata key `fork_source_context_seq`. Rows
  that receive a `context_seq` later can never move into or out of the prefix. A released fork without that key
  (forked from a non-`avibe` Session) has no Avibe Agent context to inherit and starts empty.
- The child's context = the source's context rows with `context_seq <= anchor_seq` (recursively through the source's
  own fork), then the child's rows.
- The child's `context_seq` continues from `anchor_seq + 1`, so the combined order stays total.
- Nothing is copied. A checkpoint in the source after `anchor_seq` is invisible to the child.

## 5. Retention and deletion

- Trace retention never selects `visibility = 'context'`; a contract test pins this.
- The context content of a row never changes after commit: `context_seq` and `content_json.model` are written once
  (on an input row, when it is consumed). A response row's display columns (§2) may be written again when it is
  delivered; they are never context. Context rows are never deleted while a Session or a fork descendant references
  them.
