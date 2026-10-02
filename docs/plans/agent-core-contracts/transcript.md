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
| model response | `messages` | `assistant` (not final), `result` (final) | — | `content_json.model` = `ModelResponse` |
| tool result | `agent_events` | `tool_result` | `context` | `content_json` = `ToolResult` |
| checkpoint | `agent_events` | `context_compaction` | `context` | `content_json` = `Compaction` |
| cleared result | `agent_events` | `context_edit` | `context` | `content_json` = `ContextEdit` |
| hook state | `agent_events` | `agent_state` | `context` | `content_json` = `AgentState` |

Display-only rows keep `context_seq` null: `interim`, `notify`, `error`, `vault`, `output`, queued or removed inputs,
and the `tool_call` trace row written at tool start (its `metadata_json` carries `tool_call_id` and `job_id` so the
activity panel can pair it with the result).

## 2. Writing

- The loop is the only writer of a Session's `context_seq`, under a per-Session lock held across the transaction:
  `next = max(context_seq of the Session in both tables) + 1`.
- Inputs already exist as rows when they are submitted. The loop sets `context_seq` and `content_json.model` on that
  row when it consumes the input, in the same transaction that commits the previous step if there is one.
- A response row is inserted at `message_end` with both `content_json.model` and its rendered `content_text`.
- A tool result row is inserted at `tool_finished`. The `tool_call` trace row is inserted at tool start without a
  `context_seq`.
- Each commit is one SQLite transaction. The adapter delivers a row to surfaces only after it commits; the
  dispatcher does not persist it again.
- **Output outbox.** An `assistant` or `result` row is committed with `metadata_json.delivery = {"state":
  "pending"}` in the same transaction. After the surface accepts it, the adapter sets `{"state": "delivered"}` with
  the platform receipt (`native_message_id` where the platform returns one). At startup, and before a Session
  resumes, the adapter re-delivers its `pending` rows in `context_seq` order; delivery is idempotent per row id, so a
  crash between commit and dispatch neither loses nor regenerates the response.

## 3. Projection

`project(session_id, fork_point=None) -> list[Message]`, a pure function of the rows:

1. Collect the context rows: the Session's own, plus its fork ancestry (§4).
2. Order by `context_seq`.
3. If a `context_compaction` row exists, take the latest; the context becomes its checkpoint message followed by the
   rows with `context_seq >= first_kept_seq`, excluding older checkpoints and `agent_state` rows.
4. Apply `context_edit` rows: the latest edit per target replaces that tool result's content with its placeholder.
5. Settle tool calls without a result: if the call's job exists (C-7), its current status becomes the result
   ("still running, now Watch …" or its final output); otherwise the synthetic interrupted result from
   `cross-provider.md`.
6. Prepend the rebuilt system prompt and rehydrated state (plan §5.2); these are not rows.

## 4. Fork

The child Session's metadata already records its parent as top-level keys `fork_source_session_id` and
`fork_source_message_id` (written by `reserve_forked_session`, read by `fork_metadata_from_session_metadata` in
`core/services/session_fork.py`); C-5 reads those keys and adds no new fork shape.

- `anchor_seq` = the largest `context_seq` in the source Session among rows at or before the anchor message in
  transcript order (the anchor itself may be a display-only row).
- The child's context = the source's context rows with `context_seq <= anchor_seq` (recursively through the source's
  own fork), then the child's rows.
- The child's `context_seq` continues from `anchor_seq + 1`, so the combined order stays total.
- Nothing is copied. A checkpoint in the source after `anchor_seq` is invisible to the child.

## 5. Retention and deletion

- Trace retention never selects `visibility = 'context'`; a contract test pins this.
- Context rows are never updated after commit, except setting `context_seq` and `content_json.model` on an input row
  once, and never deleted while a Session or a fork descendant references them.
