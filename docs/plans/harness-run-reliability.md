# Harness Run Reliability

Status (2026-08-03): **PR1, PR2, PR5, and PR6 are complete. PR3 and PR4
remain. PR7 must be re-baselined against the durable Delivery/Turn model before
implementation.**

This is the execution plan, not the investigation log. The original detailed
diagnosis and its review history remain available in Git before `fe821905`.
Resolve code references by symbol against current `master`; do not port old line
numbers or old ownership assumptions.

## 1. Current baseline

| Capability | Result |
|---|---|
| Result text for delivered Harness runs | **#1063 merged** |
| Pinned-session reclaim and reservation hardening | **#1064 merged** |
| Durable, user-visible failure notices | **#1072 merged** |
| Delivery / Turn / Message ownership model | **#1134 merged** |
| Teardown-interrupted Run settlement | **#1140 merged**; supersedes closed #1131 |
| Idle-eviction interlock for queued work | **Open — PR3** |
| Bounded and supervised recovered-output drain | **Open — PR4** |
| Scheduled/watch terminal-time truth and cron liveness | **Re-baseline — PR7** |

The post-plan architecture is load-bearing:

- `message_deliveries` owns submitted input, FIFO position, acceptance,
  attempts, receipts, and retirement.
- `session_turns` owns native execution and terminal Turn evidence.
- `messages` is accepted communication history, not a queue.
- `agent_runs` is the Harness projection and callback/notice anchor.
- #1140 distinguishes an unstarted claim from an execution that crossed the
  running boundary. Unstarted work returns to queued; infrastructure-interrupted
  running work fails and is not replayed; explicit user Stop remains canceled.

Consequently, the old PR2 resolver design and PR7's old “claimed message row”
decision are obsolete. New work must use the current durable owners instead of
recreating pre-#1134 side maps.

## 2. Invariants

Every remaining change must preserve these rules:

1. **One durable owner per phase.** Delivery owns input before native
   acceptance; Turn owns accepted execution; Run reflects the outcome.
2. **No silent replay after execution starts.** Infrastructure interruption is
   `failed` with a structured cause and an actionable notice. User Stop is
   `canceled` and does not generate an infrastructure-failure notice.
3. **Unstarted work remains retryable.** A bare claim or queued Delivery must not
   be mislabeled as an interrupted execution.
4. **Terminal writes are guarded.** A natural terminal result, user Stop, and
   infrastructure teardown may race; the exact compare-and-set winner controls
   projections and notices.
5. **Absence of a receipt is not proof of non-delivery.** Any timeout around an
   outbound send must use durable delivery evidence before retrying.
6. **Waiting is not activity.** A real inbound message or exact Turn start may
   establish a session baseline; after that only observable assistant/tool
   progress refreshes it. Run inactivity is stricter and re-arms only from its
   exact owning Turn. A claim, queue wait, gate wait, or unrelated Run in the
   same session must not keep stuck work alive.
7. **No turn-duration timeout.** A healthy turn may run for hours. Bounds may
   apply to inactivity and post-turn delivery, never to productive execution.
8. **Failures remain visible.** Reconcile paths must use #1072's durable notice
   path; a terminal row alone is not the exit criterion.

## 3. Required pre-code baseline

Before each remaining PR, write or run a current-master reproducer. Classify the
old defect as `reproduces`, `fixed by #1134/#1140`, or `superseded by a new
owner`. Do not preserve an old prescription merely because the symptom still has
the same name.

Minimum baseline cases:

- a Delivery in each live ownership role (claimable, fence, and turn-owned)
  targets a session that reaches both passes of idle eviction, while terminal
  Delivery history does not pin it;
- each backend-specific idle eviction path is classified as requiring the shared
  owner interlock or as restart-safe for queued work;
- recovered Activity output delivery hangs while a new Harness request becomes
  ready;
- scheduled and watch Runs transfer to a durable Delivery/Turn and later produce
  success, failure, and result-less terminal outcomes;
- definition health is observed before and after the owning Run becomes
  terminal;
- scheduler shutdown/restart distinguishes unstarted, running, naturally
  terminal, and user-stopped work.

## 4. PR3 — Idle-eviction interlock

### Goal

Do not evict a session while durable queued or in-flight work legitimately owns
it. Do not let broken bindings or fake activity create immortal sessions.

### Required behavior

1. Build one provider that resolves the **current durable owners** of a session:
   Delivery rows whose `DELIVERY_STATE_MATRIX` ordering role is `claimable`,
   `fence`, or `turn_owned`; Turns in `waiting`, `starting`, or `active`; and any
   nonterminal Run whose exact ownership is not already represented by those
   rows. Consume the state policy instead of hard-coding Delivery state names:
   terminal `accepted` / `retired` Delivery history does not pin a session,
   while unresolved fences and turn-owned states do. Reuse current Delivery/Turn
   storage and teardown APIs; do not restore PR2's discarded in-memory resolver
   design.
   Inventory Claude, Codex, and OpenCode eviction consumers; each path that can
   invalidate a durable target must consume this provider, while a restart-safe
   transport cache needs an explicit test rather than a duplicate interlock.
2. Consult the provider in both passes of `evict_idle_sessions`. Recompute during
   the second pass so work admitted between the two reads pins the session.
3. Use two failure modes:
   - a lookup that positively proves one binding is dangling fails open for only
     that binding, so a deleted target cannot pin an unrelated session forever;
   - any exception while resolving a binding, or any provider-wide failure,
     fails closed for the eviction cycle, because missing safety data is not
     evidence that eviction is safe.
4. Bound the interlock with the existing real-progress inactivity clock and
   stuck-active threshold. A newly admitted pin does not restart that clock.
   Beyond the bound, use the #1140 teardown path to settle exact running
   ownership and preserve queued/unstarted work.
5. Do not touch `session_last_activity` on claim, enqueue, gate wait, or provider
   lookup. Real inbound/Turn-start baselines and subsequent assistant/tool
   progress remain the liveness signals.

### Required evidence

- queued work pins the exact target session;
- unresolved fence and turn-owned Delivery states pin, while terminal Delivery
  history without a nonterminal Turn does not;
- a pin admitted between eviction passes wins;
- unrelated sessions are not pinned;
- one positively missing binding fails open;
- a per-binding lookup exception or provider failure aborts the cycle;
- repeated queued followers cannot extend the pin past the stuck threshold, and
  teardown settles only exact running ownership;
- a claimed or gate-waiting request does not refresh activity;
- a long turn with observable progress is not evicted;
- every enabled backend eviction path either honors the provider or proves queued
  work resumes without loss or replay.

Exit criterion: no queued work is lost or misclassified, no productive turn is
timed out, and the interlock cannot make a stuck session immortal.

## 5. PR4 — Bound and supervise recovered-output delivery

### Goal

One hung post-turn delivery must not block `_watch_store` or every other Harness
tenant. This is the direct fix for the observed 65-minute stall.

### Required behavior

1. Move `_drain_recovered_activity_outputs` off the `_watch_store` critical path,
   or bound it there. A detached drain must still be tracked, single-flight,
   time-bounded, and canceled/awaited during service stop.
2. Bound post-turn delivery, not Agent execution. The no-turn-duration-timeout
   invariant remains unchanged.
3. Add one durable, Activity-owned output-attempt record keyed by the stable
   `MessageOutput.idempotency_key`. It must survive restart without a Run row and
   carry guarded `pending` / `sending` / `delivered` / `failed` / `acknowledged` /
   `abandoned` phase, attempts, `next_attempt_at`, returned delivery id or
   persisted receipt, and terminal error. Persist intent before the transport
   call and acknowledge/delete the Activity snapshot only from a guarded
   terminal transition.
4. Extend the structured `DeliveryEvidence` plumbing through the `result` path
   used here; today `core/delivery_evidence.py` is notification-oriented and
   in-memory. Treat it as evidence for the current attempt, not as a substitute
   for the durable Activity record. Before retrying, also consult the stable
   output id and any persisted message receipt. Never implement
   `except TimeoutError: requeue` from absence of a return value alone.
   Preserve the existing target-class evidence rule: a real IM delivery id may
   be `delivery_only` evidence, but an Avibe synthetic id is not acknowledgement
   without a persisted receipt.
5. Keep ambiguous post-send retries bounded: retry at most once when a send may
   have succeeded, then move the same Activity-owned record to a durable failure
   state. Delivery of that failure notice has its own bounded backoff and ends in
   `acknowledged` or operator-visible `abandoned`; either terminal releases the
   Activity claim and settles a linked deferred Run without requiring one.
   Surface `failed` / `abandoned` through the session Activity projection and,
   when linked, the Harness Run detail.
6. Re-arm `_drain_dirty` for every temporary request/callback skip. Persist retry
   timing and apply backoff to unrunnable work so a retry cannot hot-spin on the
   two-second tick.
7. Add a heartbeat plus a separately tracked supervisor task that is not awaited
   by `_watch_store`. It must identify the overdue drain in a loud log; a watchdog
   running inside the blocked loop cannot diagnose that loop.

### Required evidence

- a hung recovered-output send does not delay request/callback draining;
- only one instance of each drain can run;
- service stop cancels and joins owned drain tasks;
- a definitive failure before transport invocation retries safely;
- timeout after confirmed delivery does not resend;
- a real IM id with no persisted row acknowledges without resending, while an
  Avibe synthetic id with no persisted row remains unacknowledged;
- cancellation during or after the transport call remains ambiguous unless
  structured evidence or the stable receipt proves an outcome;
- a crash between send and persistence resumes from the durable attempt record;
- ambiguous post-send state has a bounded retry and durable terminal outcome;
- work with no Run row still reaches an Activity-owned terminal outcome visible
  from its session;
- every skip re-arms with backoff;
- the independent watchdog reports which owned drain is overdue.

Exit criterion: the watch loop continues to make progress under a hung transport,
and every claimed output reaches acknowledged, safely retryable, or explicit
terminal state.

## 6. PR7 — Re-baseline terminal-time truth; then split

Do **not** implement the old PR7 prescription. #1134 added
`complete_on_return`, durable Delivery ownership, immutable Turn terminal
evidence, and restart recovery. First determine what remains.

### PR7A — Run and definition truth

1. Prove whether scheduled and watch Runs already remain nonterminal after
   Delivery ownership transfers and settle from the exact terminal Turn/result.
   If #1134 already fixed the Run timing, close that part with regression tests
   instead of adding another writer.
2. Verify success, failure, result-less termination, user Stop, and terminal-write
   failure in both the direct IM execution lane and the Workbench durable
   `SessionTurnManager` lane. Exactly one terminal result wins in each; the
   Workbench path must have exactly one terminal writer, and a failed write must
   not strand the Run or the Turn waiter.
3. Keep `health`, `consecutive_failures`, and `recent_failures` derived from the
   existing bounded terminal Run history; do not add a mutable health projection
   or cursor. Scope terminal-time projection to compatibility fields such as
   `last_run_at` / `last_error` and one-shot lifecycle updates. Make the Run CAS
   and those updates atomic where they share a transaction, or reconcile them
   idempotently with monotonic `(completed_at, run_id)` ordering after a crash;
   replay of an older event must not overwrite a newer compatibility projection.
   Dispatch or Delivery acceptance is not task success.
4. If pre-fix rows still match the old premature-success signature — scheduled
   or watch `status=succeeded`, empty `result_text`, and `completed_at` within the
   dispatch-time window of `created_at` — stamp
   `metadata.pre_settlement_migration=true` and render a quiet “legacy — delivery
   only” marker. Do not mark honest failures, cancellations, or later terminal
   results. Route UI copy through `ui/src/i18n/en.json` and
   `ui/src/i18n/zh.json`, and CLI/backend copy through the matching
   `vibe/i18n/` catalogs; do not hardcode either locale. Do not rewrite
   historical status or invent result text.

### PR7B — Cron liveness

1. Scheduler fire is enqueue-only; APScheduler must not hold `max_instances=1`
   for the duration of an Agent turn. Preserve one-pending-fire-per-definition
   coalescing so a productive long turn gains one queued successor, not an
   unbounded backlog.
2. Honor a durable, per-Run **inactivity** limit for scheduled runs. Start it at
   enqueue and persist `last_progress_at` only from observable assistant/tool
   output belonging to the exact Turn; if a Turn owns several coalesced Runs,
   re-arm those exact Runs. Queued or stalled work ages. Do not derive this from
   `session_last_activity`, generic `agent_runs.updated_at`, or progress from an
   unrelated Run sharing the session.
3. On expiry, atomically record `metadata.interrupt_reason=lifetime_timeout`
   before cancellation. First handle a Run with no Delivery under the same
   guarded ordering boundary used by `reserve_delivery`: retire its queued or
   bare-claimed request ownership, CAS the Run to failed with
   `lifetime_timeout`, release its pending-fire coalescing slot, and emit the
   durable notice without claiming that execution was interrupted. If this CAS
   loses to Delivery creation, reload and consume `DELIVERY_STATE_MATRIX` for
   **every** Delivery state; do not branch only on queued versus accepted. A
   `run_cancel=retire` state may retire only work the policy proves unwritten. A
   `run_cancel=turn_owner` state routes through its exact Turn owner's cause-aware
   cancellation/reconciliation; if no Turn exists, preserve the Delivery's
   possible/unknown native-effect evidence and reconcile it as an infrastructure
   failure without replay or an “unwritten” claim. The previously recorded
   timeout cause deliberately overrides normal restart's one-time unknown-start
   replay. A terminal `run_cancel=complete` Delivery is not automatically a
   no-op: cancel its linked nonterminal Turn, reconcile from its linked terminal
   Turn, or fail an inconsistent ownerless projection durably. Every resulting
   failure settles visibly through the notice path.
4. Cover delivery-less queued and bare-claimed requests, including a race with
   `reserve_delivery`; cover `claimed`, `pending_steer`, `steering`,
   `interrupt_waiting`, `reconciling_steer`, and `accepted`, with `accepted` in
   both nonterminal and terminal linked-Turn cases. Each must reach its exact
   owner or a durable terminal ambiguity outcome, release its request claim,
   coalescing slot, or ordering fence as applicable, and never replay work with
   possible/unknown native effects.
5. Reuse `run_definitions.lifetime_timeout_seconds` as the per-task override and
   store a 1,800-second global inactivity default in `config/v2_config.py`. For
   a recurring cron Run, snapshot at enqueue:
   `min(positive_override_or_global_default, 0.8 × next_fire_gap)`, where
   `next_fire_gap` is the duration from this Run's enqueue/fire instant to the
   same timezone-aware trigger's actual next fire, not an average parsed from the
   expression. An absent or zero cron override means “use the global default,”
   not “disable”; reject negatives. For a one-shot `at` Run, use the positive
   override or global default without a recurrence ceiling. The bound still
   measures inactivity, not duration.
   Thread the scheduled-task override through add/update CLI and API payloads and
   the Web task form, without changing the existing watch lifetime semantics.
6. Cover unset, zero, shorter, and longer overrides; irregular cron schedules and
   a DST boundary; a one-shot `at` Run; progress from another Run in the same
   session; multiple fires during one productive Turn; add/update round trips;
   unchanged watch behavior; and restart reconstruction. Pin both sides of the
   ambiguity policy: ordinary restart may replay an unknown start once, while a
   Run already expired as `lifetime_timeout` never replays. A long override must
   clamp before the actual next cron fire without canceling an exact owner that
   keeps producing observable progress.

The old D7 blocker is retired by an explicit split, not by claiming ambiguity
vanished. Normal crash recovery preserves #1134's bounded one-time replay for a
`starting` Turn whose start receipt is `unknown`. PR7B inactivity expiry is a
different, cause-first terminal decision: once `lifetime_timeout` wins, the same
unknown evidence fails visibly and never replays. Unstarted work remains
retryable; known-started work interrupted by infrastructure fails; and
natural/user-stop compare-and-set winners prevail.

Exit criterion: Run status, definition health, scheduler availability, and user
notifications all describe the same terminal event without replaying accepted
work or timing out productive turns.

## 7. Order and review boundaries

```text
complete: #1063, #1064, #1072, #1134, #1140
    |
    v
PR3 eviction interlock
    |
    v
PR4 recovered-output drain
    |
    v
PR7A terminal-time truth
    |
    v
PR7B cron liveness
```

PR7A and PR7B are separate review units unless the re-baseline proves one is
already complete. Keep PR3 and PR4 separate: PR3 protects session ownership;
PR4 fixes the global liveness failure.

Scenario ranges reserved by the original plan remain available on current
`master`:

- PR3: `HFR-130…154`
- PR4: `HFR-155…179`
- PR7: `HFR-180…219`

Check the catalog again immediately before coding. If a range has been occupied,
allocate a fresh contiguous block above the highest merged ID; never reuse a
closed branch's overflow table.

## 8. Validation and non-goals

For every PR:

- add unit and contract tests first against the real owner boundary;
- add/update `tests/scenarios/harness_failure_recovery/catalog.yaml`;
- assert both the durable state and the user-visible notice where failure is
  expected;
- use guarded writers and test the losing race, not only the happy winner;
- keep tests hermetic; do not write real `~/.avibe` state;
- route new backend and frontend display copy through the matching English and
  Chinese i18n catalogs;
- run Ruff on changed Python files;
- for any UI change, run `cd ui && npm run build` and verify the packaged
  `ui/dist` contains the intended frontend change;
- use the local Incus regression runner for cross-platform verification;
- never restart the local `vibe` service for routine validation.

Non-goals:

- a new Run status value such as `interrupted`;
- backend-level exactly-once execution;
- an absolute Agent turn-duration timeout;
- widening this work into unrelated session/process lifecycle cleanup;
- preserving old implementation details that #1134 or #1140 superseded.
