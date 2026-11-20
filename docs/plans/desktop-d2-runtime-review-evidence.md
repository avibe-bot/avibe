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
| A4–A5 | Transcript expression/time range; Delivery session/live-state range, independent of submission time. Legacy clicks skip freshness | `test_answer_freshness_queries_are_bounded_by_live_work`; `test_legacy_can_answer_an_older_unanswered_group` |
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
errors. Unmatched targets/options fall through to the original ordinary dispatch and response, without reserving an answer operation. Three real HTTP regressions failed on the previous head (404/400/400 instead of dispatch success) and pass after restoring this fallback. Native requests keep revision/freshness checks and durable replay. The
crash window after controller acceptance but before acknowledgement is an
explicit limitation: ownership prevents duplicate dispatch, while chosen/SSE
completion waits for the original operation to replay.

Migration 0092 remains unreleased and additive; its new-table index now includes
the Delivery key. This uses the guard's supported additive index form rather than
extending migration-policy syntax. Existing tables and compatibility epoch are unchanged.

Independent review follow-up: kinds come from the Runtime-served page, so existing I3 iPhone bridges adopt filtering on Runtime update. Two real-storage cases prove unsupported-by-current-device requests remain pending through 299 seconds, can be claimed by a capable device, or expire at the default 300 seconds. No global capability registry or early unsupported inference is added. App-only rollback documentation explicitly records the empty text-answer marker being treated as unanswered by older Runtime code.

## Third-round lifecycle and draft closure

The lead accepted the independent state/event table in `review-round3-design.md`.
Freshness uses acceptance order only: every live user input precedes a newly
submitted answer, except a still-claimed start input of the turn that produced
the question. The target turn is read with the existing guarded Message turn-ID
SQL expression. A stuck reserved input therefore keeps native answers stale until
recovery; legacy answer groups retain their approved freshness exemption.
Draft effects use the original request action, separately from answer reservation.

C matrix: old queued input, its accepted stage, retirement, the question turn's
claimed start, other-turn claimed input, a reserved fence, legacy old queued input,
and real EXPLAIN with 250 accepted plus 250 retired history rows and one live row.
The pending lookup uses `ix_message_deliveries_session_state (session_id=? AND
state=?)`, without a submission-time predicate, SCAN, or temporary B-tree.
D matrix: three legacy passthrough cases preserve a nonempty stored draft and
return `draft_advanced: false`; ordinary composer send advances it; native text
answers preserve it. HTTP, SQLite, and controller admission remain real.

On 5d0cdcc the matrix exposed seven failures (three live-input lifecycle cases,
the pending query/behavior case, and three fallback draft cases). The same answer
suite passes all 38 cases after the correction. Existing accepted-input, own-turn,
retired-input, legacy exemption, composer-send, and text-answer controls stay green.

Final related validation: 303 tests passed across answer authority, session-stream routes, message storage, API inventory, and all scenario citations. Ruff passed for all changed Python files. No additional migration or UI change was needed.
