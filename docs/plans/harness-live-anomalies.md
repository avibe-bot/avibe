# Harness Live and Anomaly View

## Background

Issue #1060 originally grouped several Harness reliability gaps. The merged
implementation has since split their ownership:

- #1005 reconciles abandoned queued/running Runs to terminal outcomes.
- #1098 implements explicit send-now and queue controls from #1095.
- #1072 and #1223 own failure delivery plus Watch waiter/processing health.
- #1155 and #1173 own durable runtime ownership and supervised work lanes.
- #1331 keeps cancellation scoped to the exact shared-Turn participant.

The remaining operator gap is narrower: a live Run has no persisted activity
fact between start and completion, and no command combines current Harness work
with explicit anomalies. Concurrent active agents using one workdir are visible
only by manually comparing runtime rows.

## Goal

Provide one read-only operational view while preserving the existing lifecycle,
delivery, cancellation, and runtime ownership models.

## Design

1. Persist `metadata.last_activity_at` on non-terminal Run output. The write is
   guarded to active Run states and advances both the fact and `updated_at`
   monotonically. Existing stale-Run reconciliation therefore measures its grace
   from the latest observed activity without gaining another timer or owner.
2. Extend the controller's existing running-agent snapshot with the exact Run ids
   already owned by `ScheduledTaskService` and `SessionTurnManager`. Failure to
   read ownership is explicit and fails closed.
3. Add `vibe harness status`. It combines active Runs, armed Watches, upcoming
   Tasks, and the controller snapshot into one bounded-by-live-state response.
4. Derive anomalies without persisting a parallel state machine:
   `controller_unavailable`, `run_owner_missing`, `watch_waiter_missing`,
   `watch_waiter_failed`, `run_ownership_unknown`, and
   `active_workdir_conflict`.

Workdir conflicts are marked, not rejected. Existing installations may
deliberately share a checkout, and the current scheduler has no durable
read-versus-write declaration that would make a rejection truthful. Two active
runtime rows with the same canonical workdir are nevertheless an explicit
operator anomaly and every affected row is flagged.

## Non-goals

- Reimplement send-now, cancellation, failure notices, Watch supervision, or
  runtime ownership.
- Add a new Run status or a second durable liveness table.
- Infer that a long-running but owned Run is dead solely from elapsed time.
- Change the Workbench Harness UI in this PR.

## Evidence

- Unit: monotonic activity writes and terminal-state guards.
- Contract: controller ownership projection and unified anomaly derivation.
- Scenario: HFR-480 (persisted Run activity and owner loss) and HFR-481
  (same-workdir conflict plus Watch/Task operational inventory).
- Residual manual: packaged CLI against a live multi-backend service remains an
  Incus acceptance check after review.
