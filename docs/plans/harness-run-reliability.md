# Harness Run Reliability

Status (2026-08-03): **PR1, PR2, PR5, PR6, and #1139's Activity-output
settlement closure are complete. PR3 and PR4's shared-drain liveness work
remain. PR4's transport-attempt delta and PR7 must be re-baselined against the
durable Delivery/Turn/Activity model before implementation.**

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
| Activity output batch receipt and local settlement | **#1139 merged**; supersedes #1121 |
| Idle-eviction interlock for queued work | **Open — PR3** |
| Bounded and supervised shared drains | **Open — PR4**; attempt-state delta requires a current-master reproducer |
| Scheduled/watch terminal-time truth and cron liveness | **Re-baseline — PR7** |

The post-plan architecture is load-bearing:

- `message_deliveries` owns submitted input, FIFO position, acceptance,
  attempts, receipts, and retirement.
- `session_turns` owns native execution and terminal Turn evidence.
- `messages` is accepted communication history, not a queue.
- durable Activity snapshots own pending output payload, ordered batch
  membership, the linked Run union, and the stable output receipt identity.
  Accepted Message evidence or a persisted Activity batch marked for local
  settlement only prevents transport replay; a delivered output that still owes
  local settlement retries only that settlement.
- `agent_runs` is the Harness projection and callback/notice anchor.
- #1140 distinguishes an unstarted claim from an execution that crossed the
  running boundary. Unstarted work returns to queued; infrastructure-interrupted
  running work fails and is not replayed; explicit user Stop remains canceled.

Consequently, the old PR2 resolver design, PR7's old “claimed message row”
decision, and any pre-#1139 single-Activity output assumptions are obsolete. New
work must use the current durable owners instead of recreating side maps or a
parallel output ledger.

## 2. Invariants

Every remaining change must preserve these rules:

1. **One durable owner per phase.** Delivery owns input before native
   acceptance; Turn owns accepted execution; the Activity batch owns pending
   terminal output; Run reflects the outcome.
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
9. **One output batch has one receipt.** Preserve #1139's stable receipt,
   persisted ordered Activity membership, complete linked Run union, and
   transport-free local-settlement retry. Incomplete or conflicting recovered
   membership fails closed; it never emits a partial batch or invents a second
   receipt.

## 3. Required pre-code baseline

Before each remaining PR, write or run a current-master reproducer. Classify the
old defect as `reproduces`, `fixed by #1134/#1139/#1140`, or `superseded by a
new owner`. Do not preserve an old prescription merely because the symptom still
has the same name.

Minimum baseline cases:

- a Delivery in each live ownership role (claimable, fence, and turn-owned)
  targets a session that reaches both passes of idle eviction, while terminal
  Delivery history does not pin it;
- each backend-specific idle eviction path is classified as requiring the shared
  owner interlock or as restart-safe for queued work;
- a complete #1139 Activity output batch with multiple Activities and linked
  Runs is restored in its persisted order; accepted-Message evidence and the
  persisted local-settlement-only marker each prevent transport replay while
  local settlement is retried;
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
   nonterminal **execution-bearing** Run whose exact ownership is not already
   represented by those rows. Reuse one explicit Run-type classification with
   recovery/health: exclude `run_type=watch_runtime` supervisor heartbeats, and
   fail closed on unknown Run types until they are deliberately classified; a
   definition/session binding alone is not execution ownership. Consume the
   state policy instead of hard-coding Delivery state names:
   terminal `accepted` / `retired` Delivery history does not pin a session,
   while unresolved fences and turn-owned states do. Reuse current Delivery/Turn
   storage and teardown APIs; do not restore PR2's discarded in-memory resolver
   design. Resolve the Delivery, Turn, and fallback Run union from one SQLite
   read snapshot. If an implementation must cross store handles, fence the read
   with a monotonic storage version and fail closed / retry when it changes; it
   must never decide that a Run is represented by a Delivery omitted from the
   same snapshot.
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
- a `watch_runtime` heartbeat sharing the same definition/session does not pin,
  while an execution-bearing watch Run does;
- a pin admitted between eviction passes wins;
- bare-Run to reserved-Delivery and Delivery to Turn ownership handoffs cannot
  disappear across a torn provider read;
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

## 5. PR4 — Bound and supervise shared drains

### Goal

One hung or unbounded request, Run-callback, vault-callback, or post-turn-output
pass must not block `_watch_store` or every other Harness tenant. This is the
direct fix for the observed 65-minute stall and the same critical-path shape in
the other inline drains. #1139 already owns output batch identity,
anti-redelivery evidence, and post-delivery local settlement; PR4 must preserve
that owner and fix only the remaining shared-loop and transport-attempt gaps.

### Required behavior

1. Apply one invariant to every drain invoked by `_watch_store`:
   `_drain_requests`, `_drain_callbacks`, `_drain_vault_callbacks`, and
   `_drain_recovered_activity_outputs` must each run as a bounded page or as a
   separately tracked task outside the critical path. Re-arm when a page leaves
   work; in particular, do not call `list_pending_request_callbacks` without a
   limit or scan an unbounded skipped-request backlog. A detached drain must be
   single-flight, time-bounded, and canceled/awaited during service stop. Keep
   only provably nonblocking in-memory decisions inline. Every storage touch on
   the loop, including `store.maybe_reload()`, `request_store.maybe_reload()`,
   and `_sweep_stale_runs()`, must use an async-compatible store path or a
   bounded worker / separately tracked task with the same single-flight,
   timeout, re-arm, and shutdown-join guarantees. An implementation may keep a
   probe inline only when a contention test proves it cannot wait on a database
   lock or storage I/O. Merely wrapping synchronous storage in `create_task` is
   not isolation: a stalled operation must not block the event loop or prevent
   the stale-run lane from making independent progress. Timeout or cancellation
   of an async wrapper does not release single-flight ownership while its
   synchronous worker is still running. Keep the underlying worker future as the
   lane owner, quarantine that lane until it exits, and join it before disposing
   service state; never launch a replacement against the same store merely
   because the wrapper timed out.
2. Bound drain work, not Agent execution. The no-turn-duration-timeout invariant
   remains unchanged.
3. Treat #1139's persisted Activity batch as the sole owner of output payload,
   ordered Activity membership, linked Run provenance, stable receipt identity,
   and post-delivery local settlement. Start with a red current-master test for
   the remaining ambiguous transport window. Do not create a second output
   ledger, copy batch content or membership into an attempt record, or let a
   drain become a second settlement owner.
4. Only if that reproducer proves durable operational evidence is still missing,
   add one batch-level transport-attempt record keyed by the stable
   `MessageOutput.idempotency_key` inside the existing Activity runtime-record
   aggregate. It may carry only guarded `pending` / `sending` / `delivered` /
   `failed` / `acknowledged` / `abandoned` phase, attempts, `next_attempt_at`,
   returned delivery id or persisted receipt, terminal error, and a reference to
   the existing output batch. `SessionActivityRegistry` and its store remain the
   single transition owner. Persist intent before the transport call and
   acknowledge/delete the Activity snapshots only from a guarded terminal
   transition.
5. Extend the structured `DeliveryEvidence` plumbing through the `result` path
   used here; today `core/delivery_evidence.py` is notification-oriented and
   in-memory. Treat it as evidence for the current attempt, not as a substitute
   for the durable Activity batch and batch-level attempt state. Before retrying,
   also consult the stable output id, accepted Message, persisted
   local-settlement-only marker, and any persisted transport receipt. Never implement
   `except TimeoutError: requeue` from absence of a return value alone.
   Preserve the existing target-class evidence rule: a real IM delivery id may
   be `delivery_only` evidence, but an Avibe synthetic id is not acknowledgement
   without a persisted receipt.
6. Keep ambiguous post-send retries bounded: retry at most once when a send may
   have succeeded, then move the same Activity-owned record to a durable failure
   state. Delivery of that failure notice has its own bounded backoff and ends in
   `acknowledged` or operator-visible `abandoned`; either terminal releases the
   Activity claim and settles a linked deferred Run without requiring one.
   Surface `failed` / `abandoned` through the session Activity projection and,
   when linked, the Harness Run detail.
7. Do not rely on a SQLite data-version edge after `maybe_reload()` as the only
   trigger for any detached drain. Probe each lane independently every tick, or
   give it durable `next_attempt_at` eligibility. Its task-completion path must
   re-arm after timeout, cancellation, or exception as well as after a partial
   page or temporary request/callback skip; only service shutdown suppresses
   re-arming. Persist retry timing and apply bounded backoff to unrunnable or
   repeatedly failing work so retries cannot hot-spin on the two-second tick.
8. Add a heartbeat plus one separately tracked supervisor task per service
   instance that is not awaited by `_watch_store`. It must identify the overdue
   drain in a loud log; a watchdog running inside the blocked loop cannot diagnose
   that loop. Cancel and await the supervisor alongside the owned drain tasks in
   both stop and restart paths so repeated starts cannot accumulate stale
   watchdogs or false overdue-drain logs.

### Required evidence

- a hung request-admission store call, Run-callback lookup/enqueue,
  vault-callback storage/dispatch operation, or recovered-output send does not
  delay the other drains or stale-run sweeps;
- a contended reload probe or stale-run store operation does not block the event
  loop, suppress independent drain progress, or serialize unrelated tenants;
- a timed-out synchronous worker remains the sole lane owner until its real
  future exits; no overlapping retry starts, and shutdown joins or explicitly
  quarantines it before store disposal;
- large request, Run-callback, and vault-callback backlogs drain in bounded pages
  and reliably re-arm;
- after `maybe_reload()` consumes the only store change, timeout, cancellation,
  and exception completion each re-arm remaining work with backoff; shutdown
  cancellation does not re-arm;
- only one instance of each drain can run;
- service stop/restart cancels and joins owned drain tasks and the supervisor;
- repeated start/stop cycles leave exactly one current supervisor and no stale
  overdue-drain logs;
- a definitive failure before transport invocation retries safely;
- timeout after confirmed delivery does not resend;
- a complete multi-Activity batch preserves its persisted order, one stable
  receipt, and the complete Run union through timeout, restart, and local
  settlement failure;
- an accepted Message or persisted Activity-batch local-settlement-only marker
  suppresses transport replay, including after restart;
- incomplete or conflicting recovered batch membership fails closed before
  transport and remains under the same Activity owner;
- a real IM id with no persisted row acknowledges without resending, while an
  Avibe synthetic id with no persisted row remains unacknowledged;
- cancellation during or after the transport call remains ambiguous unless
  structured evidence or the stable receipt proves an outcome;
- if the current-master reproducer proves a durable attempt record is necessary,
  a crash between send and persistence resumes from that record; otherwise an
  equivalent crash/restart test must prove the existing Activity receipt and
  local-settlement-only marker close the window without adding one;
- ambiguous post-send state has a bounded retry and durable terminal outcome;
- work with no Run row still reaches an Activity-owned terminal outcome visible
  from its session;
- every skip re-arms with backoff;
- the independent watchdog reports which owned drain is overdue.

Exit criterion: the watch loop continues to make progress under a hung drain
operation, every claimed output reaches acknowledged, safely retryable, or
explicit terminal state, and no request or callback backlog can monopolize a
pass.

## 6. PR7 — Evidence gate before any new timeout model

Do **not** implement the old PR7 prescription. #1134 added
`complete_on_return`, durable Delivery ownership, immutable Turn terminal
evidence, and restart recovery; #1139 added exact Activity output batch receipts,
Run-union settlement, anti-redelivery evidence, and transport-free local
settlement retry; #1140 closed teardown-interrupted Run settlement. The previous
plan guessed at the remaining timeout model before proving which gaps still
exist. PR7 starts with evidence, not a schema or writer.

### PR7R — Current-master evidence

PR7R changes tests and the plan only. It must publish one executable matrix for:

- Claude, Codex, and OpenCode;
- direct IM and durable Workbench execution lanes;
- scheduler cron fires, one-shot `at` fires, manual CLI/API task runs, and watch
  Runs;
- success, failure, result-less termination, user Stop, terminal persistence
  failure, pending output delivery, and post-delivery local settlement failure.

For every cell, trace the exact durable facts from Run enqueue through request /
Delivery reservation, Turn start, terminal-result latch, Turn terminal evidence,
Activity output batch, accepted Message receipt, Run settlement, and definition
health projection. The matrix must answer these questions with a consuming test,
not prose inference:

1. Does the Run remain nonterminal until its actual terminal Turn/result or
   Activity output batch settles it? If yes, close the old premature-success
   claim with regression evidence instead of adding another writer.
2. Which observable assistant/tool events can be attributed to the exact Turn
   and participating Runs? Prove this separately for every backend and both
   lanes. A backend/lane without an exact signal blocks a generic inactivity
   timeout; session-wide activity is never an acceptable substitute.
3. Can scheduler and manual Runs with different source semantics or effective
   deadlines enter the same Turn? Record the current merge key and every Run in
   the batch. Cancellation is Turn-level, so no per-Run policy may be specified
   until this cardinality is explicit.
4. Which evidence exists before the Turn becomes terminal? A terminal-result
   latch, durable pending-output fact, accepted Message, or Activity
   local-settlement-only marker proves that natural completion has started and
   must outrank a later inactivity decision.
5. Are `health`, `consecutive_failures`, `recent_failures`, `last_run_at`, and
   `last_error` already monotonic projections of bounded terminal Run history?
   Dispatch or Delivery acceptance is never task success.

Exit criterion: the checked-in matrix and tests identify each remaining defect
and its current owner. PR7R adds no status, timeout field, terminal writer,
health cursor, or cancellation path.

### Conditional PR7A — Terminal truth closure

Open PR7A only for a defect reproduced by PR7R. Route settlement through the
existing exact Turn, Activity batch, Message receipt, and Run CAS owners. Keep
Workbench's single terminal writer; make compatibility projections atomic with
the Run CAS or reconcile them idempotently using monotonic
`(completed_at, run_id)` ordering. Positive output delivery remains final when
later Message, Run, Turn, or Activity local settlement fails; recovery retries
only the owed local settlement.

If current data still contains misleading pre-#1134 success rows, mark only rows
with durable writer/version evidence or `completed_at` strictly before
`2026-08-02T09:56:28Z` plus the old premature-success signature. Ambiguous rows
remain unchanged. The marker is display-only: never rewrite historical status or
invent result text, and route all visible copy through the English and Chinese
catalogs.

### Conditional PR7B — Cron liveness closure

Open PR7B only when PR7R reproduces scheduler starvation or ownerless inactive
work. Its implementation contract is:

1. Scheduler fire is enqueue-only; preserve at most one pending fire per
   definition while productive work continues.
2. Progress is exact-Turn evidence wired and tested for Claude, Codex, and
   OpenCode in both direct IM and Workbench lanes. Queuing, claims, unrelated
   session activity, and another Run's progress do not refresh it.
3. Inactivity ownership is Turn-level. Runs may share a Turn only when their
   effective inactivity policy is identical; incompatible scheduler/manual
   participants remain separate queued Turns. All compatible participants re-arm
   from the same attributed progress and settle from the same winning cause.
4. A scheduler-created recurring Run snapshots
   `min(positive_override_or_global_default, 0.8 × actual_next_fire_gap)`. A
   manual CLI/API run and a one-shot `at` run use the positive override or global
   default without a recurrence ceiling. Zero means the default and negatives
   are rejected. Snapshot the chosen policy at enqueue so a nearby cron fire
   cannot shorten a manual run.
5. Before claiming inactivity, re-read exact progress plus terminal-result latch,
   Turn, pending-output, accepted-Message, and Activity receipt evidence. Any
   natural terminal/output evidence wins even when delivery or local settlement
   is still pending. User Stop and natural completion retain their existing
   precedence.
6. The winning timeout owner consumes `DELIVERY_STATE_MATRIX` and the exact Turn
   owner. It retires only work proven unwritten, never replays possible native
   effects, resumes partial cleanup by one stable claim id, releases ordering and
   coalescing fences, and emits #1072's durable visible failure notice.
7. Cover the no-Delivery reservation race, every nonterminal Delivery role,
   terminal-latch-before-Turn-terminal, natural/Stop/timeout races, restart at
   each persisted boundary, manual execution adjacent to a cron fire, and
   incompatible Runs that must not share a Turn. Ordinary unknown-start recovery
   keeps #1134's bounded replay policy; only a previously persisted
   `lifetime_timeout` owner suppresses that replay.
8. If PR7R proves a task inactivity option is required, thread it through task
   add/update CLI and API payloads, the Web form, English and Chinese command/CLI
   and AVIBE Docs pages, and agent-facing `skills/use-avibe/SKILL.md` guidance.
   Do not change existing watch lifetime semantics.

Exit criterion: Run status, definition health, scheduler availability, and user
notifications describe one winning terminal event without replaying accepted
work, timing out a productive Turn, or letting one participant cancel siblings
that have a different policy.

## 7. Order and review boundaries

```text
complete: #1063, #1064, #1072, #1134, #1139, #1140
    |
    v
PR3 eviction interlock
    |
    v
PR4 shared-drain liveness + proven transport-attempt delta
    |
    v
PR7R current-master evidence matrix
    |
    v
conditional PR7A terminal truth / PR7B cron liveness
```

PR7R is a separate test/documentation review unit. It may close either old claim
without an implementation PR; any reproduced PR7A and PR7B defects remain
separate implementation units. Keep PR3 and PR4 separate: PR3 protects session
ownership; PR4 fixes the global liveness failure.

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
- name the assigned HFR scenario ID in every affected automated test and list
  all affected scenario IDs in the implementation PR description;
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
- a second Activity output ledger, receipt identity, or settlement owner beside
  #1139's persisted Activity batch;
- backend-level exactly-once execution;
- an absolute Agent turn-duration timeout;
- widening this work into unrelated session/process lifecycle cleanup;
- preserving old implementation details that #1134, #1139, or #1140
  superseded.
