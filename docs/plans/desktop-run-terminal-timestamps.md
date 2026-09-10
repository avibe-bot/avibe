# G4 terminal run timestamp producer

## Contract and scope

The authoritative contract is `desktop-notifications-sse.md`, specifically
`"Background" for run.terminal` and `Python changes in v1`. The desktop consumer
needs durable duration, not elapsed time since event receipt. Terminal row events
carry the stored `started_at` and `completed_at` when available; missing stamps
remain missing. Non-terminal payloads do not change.

No desktop, transport, event-name, visibility, timestamp-storage, or notification
policy changes belong to this lane. The detail endpoint already exposes both
timestamps through `SQLiteBackgroundTaskStore._run_from_row`.

## Publisher inventory and design

At base `395d7c2c75980cb154bce3aa83abda7a92ff9450`, every row publisher converges on
`storage/background.py::_publish_run_rows_updated`:

- Direct callers: `cancel_run`, `claim_pending_run`, `mark_run_execution_started`,
  `update_run_status`, `mark_run_queued_from_running`,
  `recover_claimed_pre_execution_run`, `record_run_message`, `record_run_output`,
  `settle_run_terminal`, `defer_run_terminal`, and `settle_deferred_run` (including
  participant rows).
- Deferred callers use `run_update_event_transaction`; snapshots are full rows
  or fetched with `_run_rows_for_ids`, including session teardown writes.
- `core/inbox_events.py::publish_run_updated` delegates to `run_updated_payload`;
  there are no other direct callers. Add optional timestamp passthrough here.
- `core/internal_server.py` relays the supplied event without projecting it.
  `vibe/ui_server.py::_archive_publish_run_updates` sends a session invalidation,
  not a terminal run status. `core/runtime_work.py` consumes the event.

Normalize status once in the shared row publisher, then pass each stored timestamp
only when that status belongs to `TERMINAL_RUN_STATUSES`. Keep existing local bus
and controller bridge behavior unchanged, with no new reads or fabricated dates.

## Verification

- [x] Optional payload timestamps are preserved independently or omitted.
- [x] Every existing status alias preserves the non-terminal payload or adds only
  available terminal timestamps; the local bus and controller bridge agree.
- [x] Real persisted lifecycle events carry durable stamps across execution run
  types, watch-runtime rows, and an unknown future type. Completion differs from
  update time so the contract cannot silently regress to receipt/update timing.
- [x] List/detail projection and the HTTP detail endpoint expose stored timestamps,
  including nulls for incomplete history.
- [x] Focused Python tests and changed-file Ruff pass (190 tests across inbox
  events, Harness run projection, and the FastAPI UI server).

Delivery gates: exact-head Codex review, zero unresolved threads, and green CI.
Track those changing remote gates on the PR rather than in this source snapshot.

Native notification behavior remains for the Rust consumer lane and the
orchestrator's integration verification. The consumer declares `requires #<PR>`
for this producer PR; neither lane merges itself.
