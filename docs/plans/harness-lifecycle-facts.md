# Harness lifecycle facts

Status: implemented for GitHub issue #1036. Diagnosis began against
`origin/master` at `da10a846783a0aaa0932050db04a812d9667868d`.

## Goal

Make Task and Watch lifecycle state follow persisted transitions. A definition
must not become paused or finished because a clock passed, an unrelated edit
moved `updated_at`, or a later cycle overwrote the transition owner.

## Current-head diagnosis

- Pause time is deliberately absent from every projection. The UI renders the
  paused state without a timestamp, and existing tests reject `updated_at` as a
  pause time. No consumer needs `paused_at`, so this change preserves that
  smaller invariant instead of adding unused state.
- `run_at` already has one meaning at the two boundaries that interpret it: the
  scheduler and next-fire projection share `resolve_run_at`, and naive values
  use the definition's IANA timezone. Lifecycle rows and counts no longer infer
  an outcome from that clock, so they do not duplicate conversion logic.
- Before this change, one-shot Tasks became finished from a clock comparison. A manual early
  run can therefore be mistaken for the scheduled outcome, and a missed fire
  can look successful.
- A late Watch cycle preserves retirement timestamps but can still overwrite
  the exit code and error owned by the cycle that retired the Watch.
- Definition writers already publish `definitions.updated` after successful
  commits. New lifecycle writes must use the same notification.

## Model

`retired_at` remains the single definition terminal marker for both Tasks and
Watches. A nullable `retirement_reason` only distinguishes Task terminal facts
that cannot be reconstructed from run history:

- `schedule_consumed`: the scheduler atomically consumed an `at` definition and
  queued its Run;
- `schedule_missed`: APScheduler emitted a real misfire for the current stored
  one-shot schedule.

The reason does not form a second state machine. Lifecycle remains
`running | waiting | paused | finished`, declared once by the storage query.
An in-flight Run outranks retirement, then `retired_at` determines `finished`.

Manual `vibe task run` never writes either terminal fact. The scheduler enqueue
transaction writes `schedule_consumed` together with the Run, so a crash cannot
leave one without the other. Startup recovery relies on APScheduler's misfire
event, not wall-clock inference; `schedule_missed` is therefore an observed
scheduler outcome and never a fabricated success.

Run completion does not own Task retirement. This matters when the user replaces
and resumes a consumed schedule while its Run is still active: that late result
may update run history, but cannot disable or retire the replacement schedule.

A retired one-shot cannot be resumed in place: its old schedule has no future
fire to restore. Both CLI and Workbench toggles reject that transition. Updating
`run_at` or the schedule type first clears the terminal marker and creates the
new lifecycle; unrelated edits preserve it.

For Watches, once a retirement commits, `last_finished_at`, `last_exit_code`,
and `last_error` are that terminal outcome. A late cycle cannot modify them.
The cycle's individual Run remains the place where that cycle's own result is
retained.

## Migration

Add nullable `run_definitions.retirement_reason` with no backfill. Existing
`retired_at`, run history, `updated_at`, and wall-clock deadlines are not used to
invent a reason. Legacy rows therefore remain unknown until a new lifecycle
transition is observed and committed by its current owner. The one-time JSON
import preserves terminal fields that are explicitly present, but never derives
missing ones.

## Evidence

- Lifecycle contract: pause/resume/edit preservation, marker-driven Task rows
  and counts, early manual run, naive `run_at` timezone agreement.
- Scheduler scenario: atomic scheduled consumption and explicit missed-fire
  recovery.
- Watch concurrency contract: retirement outcome survives a late cycle result.
- Migration: column addition and null legacy backfill.
