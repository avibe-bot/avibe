# D2 Runtime write-path review evidence

The second reviewed head (`a67a9d14b6`) repeated the first head's unbounded-query
class in schedule claim filtering. Implementation paused for an independent
inventory and the lead's scope decision. This correction covers that entire
inventory, raw-message writeback, and legacy-client compatibility without changing
the answer or Delivery ownership model.

## Query bounds

Production route/service statements are captured with SQLAlchemy cursor events
and explained on the same real SQLite connection. No queried behavior or storage
result is mocked.

| Inventory | Bound | Consuming regression |
| --- | --- | --- |
| A1, A3, A7–A9 | Full operation/Delivery primary keys; legacy refusals have no durable operation | `test_operation_replay_is_durable_and_bound_to_input`; `test_winner_lookup_excludes_refusal_history_and_legacy_refusals_are_not_saved` |
| A2 | `ix_message_answer_operations_winner (session_id, request_id, delivery_id)` with a non-null delivery range; 200 refusal rows are excluded | `test_winner_lookup_excludes_refusal_history_and_legacy_refusals_are_not_saved` |
| A4–A5 | Transcript expression/time range; Delivery session/live-state/submission-time range. Legacy clicks skip freshness | `test_answer_freshness_queries_are_bounded_by_live_work`; `test_legacy_can_answer_an_older_unanswered_group` |
| A6/B2 | Raw message columns by message/session key, without public projection | `test_answer_preserves_committed_vibey_context_and_receipt_kind` |
| A10 | Session/Agent/resource keys; exact plans below | `test_revoked_chat_permission_precedes_operation_replay`, both request formats |
| A11 | Pending instance/action candidates; correlated operation primary key, after seeding 500 historical operations. Empty kinds skips new-create lookup | `test_kind_claim_checks_only_pending_candidates_by_operation_key`; shared claim-filter vectors |

Observed A10 plans inside the real route's writer reservation:

```text
get_session:
  SEARCH agent_sessions USING INDEX sqlite_autoindex_agent_sessions_1 (id=?)
ensure_session_agent_access:
  SEARCH agents USING INDEX sqlite_autoindex_agents_2 (normalized_name=?)
  SEARCH resource_access_policies USING INDEX sqlite_autoindex_resource_access_policies_1 (resource_kind=? AND resource_id=?)
_session_can_chat:
  SEARCH agent_sessions USING INDEX sqlite_autoindex_agent_sessions_1 (id=?)
```

The regression requires every captured authorization query to avoid a scan.
Unchanged Delivery admission/retirement and schedule recovery queries remain
outside this correction's inventory.

## Raw message writes and compatibility

B1 uses a session-scoped, set-once `json_set` update of answer fields. A question
created through `SQLiteTranscriptStore.append_response` retains its private
model payload and remains readable through the actual transcript loader after
answer acceptance; derived display fields are never persisted.

B3/B4 retain public projection only for reads and SSE publication. B5 stores
`quick_reply_answer_kind` plus an empty `quick_reply_chosen` for text, preserving
the existing string-based button lock without copying the answer text. Pet and
optimistic button state recognize the empty marker. Shared answer-kind vectors
drive Python receipts and TypeScript pet consumption.

Legacy requests preserve older unanswered groups and existing session/access
errors. Native requests keep revision/freshness checks and durable replay. The
crash window after controller acceptance but before acknowledgement is an
explicit limitation: ownership prevents duplicate dispatch, while chosen/SSE
completion waits for the original operation to replay.

Migration 0092 remains unreleased and additive; its new-table index now includes
the Delivery key. This uses the guard's supported additive index form rather than
extending migration-policy syntax. Existing tables and compatibility epoch are unchanged.
