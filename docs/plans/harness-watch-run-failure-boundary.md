# Harness Watch and Run Failure Boundary

## Background

A Watch waiter and the Agent Run created from a detected event answer different
questions:

- the waiter reports whether observation succeeded;
- the Agent Run reports whether Avibe processed the observed event successfully.

The Run must retain the Watch id as provenance, but provenance does not imply a
shared lifecycle. A successful one-shot waiter may retire normally while its
downstream Agent Turn fails.

One Agent Turn can also accept Runs from several Watch or Task definitions. A
single backend failure currently settles each linked Run and lets every Run own
an independent failure notice. That converts one user-visible failure into
several misleading Harness failures.

## Contract

### Notification ownership

The terminal Agent Turn owns the user-visible execution failure. The backend's
ordinary failure notification is the primary notification and is emitted once
per Turn, including Turns entered through Harness Runs.

Every linked Run still settles independently for audit and retry policy. Its
owed failure notice is a fallback only:

1. a persisted or externally delivered Turn notification suppresses all linked
   Run notices;
2. if the Turn notification was not acknowledged, exactly one linked Run may
   deliver the fallback;
3. the remaining linked Runs record that the same Turn was already reported;
4. if an owned Activity delays Run settlement, the Turn notification evidence
   is committed with the deferred terminal intent and survives until settlement.

The Turn id is the failure identity. Definition ids remain provenance and do
not create additional user-visible failures.

### Health ownership

For Watch definitions, `health` describes only the waiter. Downstream Agent Run
history is exposed separately as `processing_health` with independent counters.
Scheduled Tasks continue to use their existing execution health projection.
An unobserved waiter remains `unknown`; absent downstream history stays quiet in
the UI instead of being presented as a processing problem.

This is an additive projection change and requires no schema migration. Existing
Run-to-definition and Run-to-Turn links remain intact.

### User-facing copy

A successful Watch followed by a failed Agent Run is described as event
processing failure. It is not described as a Watch failure, and normal one-shot
retirement is not presented as a consequence of the Agent failure.

## Acceptance

- `HFR-436`: Watch health remains healthy after a successful waiter while its
  event processing health becomes failing.
- `HFR-437`: one acknowledged Turn failure suppresses every linked Run notice.
- `HFR-438`: a missing Turn notification produces exactly one fallback across
  all linked Runs.
- `HFR-439`: terminal Turn notification evidence reaches every linked Run in
  the same failed transition, or in the deferred terminal intent when an
  Activity still owns the Run.

Residual manual check: trigger two one-shot Watches into one failing Turn and
confirm that the conversation contains one backend error, both Runs are failed,
both Watches retire normally, and the UI separates waiter health from event
processing health.
