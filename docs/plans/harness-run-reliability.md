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
6. **Waiting is not activity.** Only observable assistant/tool progress updates
   the liveness clock. A claim, queue wait, or gate wait must not keep a stuck
   session alive forever.
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
   `fence`, or `turn_owned`; nonterminal Turns; and any nonterminal Run whose
   exact ownership is not already represented by those rows. Consume the state
   policy instead of hard-coding state names: terminal `accepted` / `retired`
   Delivery history does not pin a session, while unresolved fences and
   turn-owned states do. Reuse current Delivery/Turn storage and teardown APIs;
   do not restore PR2's discarded in-memory resolver design.
2. Consult the provider in both passes of `evict_idle_sessions`. Recompute during
   the second pass so work admitted between the two reads pins the session.
3. Use two failure modes:
   - one individually unresolvable binding fails open for that binding, so a
     dangling row cannot pin an unrelated session forever;
   - a provider-wide failure fails closed for the eviction cycle, because
     missing safety data is not evidence that eviction is safe.
4. Bound the interlock at the existing stuck-active threshold. Beyond the bound,
   use the #1140 teardown path to settle exact running ownership and preserve
   queued/unstarted work.
5. Do not touch `session_last_activity` on claim, enqueue, gate wait, or provider
   lookup. Existing real assistant/tool progress remains the liveness signal.

### Required evidence

- queued work pins the exact target session;
- unresolved fence and turn-owned Delivery states pin, while terminal Delivery
  history without a nonterminal Turn does not;
- a pin admitted between eviction passes wins;
- unrelated sessions are not pinned;
- one unresolvable binding fails open;
- provider failure aborts the cycle;
- the pin expires at the stuck threshold and teardown settles only exact running
  ownership;
- a claimed or gate-waiting request does not refresh activity;
- a long turn with observable progress is not evicted.

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
3. Reconcile a timeout using `core/delivery_evidence.py` and the current Activity
   owner. Never implement `except TimeoutError: requeue` without checking whether
   transport delivery or message persistence already succeeded.
4. Keep ambiguous post-send retries bounded. A persistent ambiguity must become
   durable, operator-visible terminal evidence and release its claim; it must not
   produce infinite duplicate notices or an immortal Activity.
5. Re-arm `_drain_dirty` for every temporary skip. Apply backoff to permanently
   unrunnable work so a retry cannot hot-spin on the two-second tick.
6. Add a per-tick heartbeat/watchdog and a loud overdue log. The next stall must
   identify the blocked drain without requiring a restart to diagnose it.

### Required evidence

- a hung recovered-output send does not delay request/callback draining;
- only one instance of each drain can run;
- service stop cancels and joins owned drain tasks;
- timeout before send retries safely;
- timeout after confirmed delivery does not resend;
- ambiguous post-send state has a bounded retry and durable terminal outcome;
- work with no Run row still reaches an Activity-owned terminal outcome;
- every skip re-arms with backoff;
- the watchdog reports an overdue tick.

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
   failure. Exactly one terminal result wins; a failed write must not strand the
   Run or the Turn waiter.
3. Project definition health only from the terminal Run compare-and-set winner.
   Dispatch or Delivery acceptance is not task success.
4. If pre-fix rows still match the old premature-success signature, stamp
   `metadata.pre_settlement_migration=true` and render a quiet “legacy — delivery
   only” marker. Route UI copy through `ui/src/i18n/en.json` and
   `ui/src/i18n/zh.json`, and CLI/backend copy through the matching
   `vibe/i18n/` catalogs; do not hardcode either locale. Do not rewrite
   historical status or invent result text.

### PR7B — Cron liveness

1. Scheduler fire is enqueue-only; APScheduler must not hold `max_instances=1`
   for the duration of an Agent turn.
2. Honor a per-run **inactivity** limit for scheduled runs. Queued or stalled work
   ages; observable assistant/tool progress re-arms the clock.
3. On expiry, record `lifetime_timeout` before cancellation and consume
   `DELIVERY_STATE_MATRIX` for **every** Delivery state; do not branch only on
   queued versus accepted. A `run_cancel=retire` state may retire only work the
   policy proves unwritten. A `run_cancel=turn_owner` state routes through its
   linked Turn's cause-aware cancellation/reconciliation; if no Turn exists,
   preserve the Delivery's possible/unknown native-effect evidence and reconcile
   it as an infrastructure failure without replay or an “unwritten” claim.
   Terminal `run_cancel=complete` states are left to their existing terminal
   owner. Every resulting failure settles visibly through the notice path.
4. Cover the intermediate `claimed`, `pending_steer`, `steering`,
   `interrupt_waiting`, and `reconciling_steer` states: each must either reach
   its exact Turn owner or a durable terminal ambiguity outcome, release its
   ordering fence, and never replay work with possible/unknown native effects.
5. Default any automatic inactivity limit below the cron interval, but do not
   turn short cron periods into absolute turn-duration caps.

The old D7 blocker is retired, not carried forward. Its premise was that a
message row simultaneously represented queued input and ambiguous execution.
That is no longer the model. Current recovery policy is the #1134/#1140 matrix:
unstarted work is retryable, started work interrupted by infrastructure fails,
and natural/user-stop compare-and-set winners prevail. Pin that matrix with tests;
raise a new product decision only if a current durable state still has genuinely
indistinguishable outcomes.

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
