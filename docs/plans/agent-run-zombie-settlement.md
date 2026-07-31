# Agent Run Zombie Settlement: honest terminal state + staleness sweep

Status: reviewed by `codex-expert` (APPROVE WITH CHANGES, 2026-07-25); all six blocking
items folded in below. **PR-A (Gap A) implemented** — see §3.4 "As implemented" and §5.1.
**PR-B (Gap B, §4) not started.**

Branch: `fix/agent-run-zombie-runs` (from `master` @ `5921ad39`)

## 1. Background and relation to the approved plan

`docs/plans/harness-run-reliability.md` (branch `fix/harness-run-reconcile`, design
approved) owns seven PRs for harness run reliability. This plan is a **delta**: two
zombie classes that inventory does not cover, both specific to
`run_type='agent_run'`.

Verified corrections carried over from that plan (do not re-litigate):

- `agent_runs.pid` is **never populated** — the only writer clears it
  (`storage/background.py:1585`). Liveness cannot be probed via `pid`.
- `recover_processing_runs` (`storage/background.py:1563-1588`) exists but
  **requeues** `running`→`queued` at controller construction; per **D1** it must
  terminalize instead. That change belongs to PR7's safety mitigations, not here.
- The status vocabulary is closed: `queued|running|succeeded|failed|canceled`.
  No new status value. Interruption is expressed as a terminal status + `error` +
  `metadata.interrupt_reason` (D1).
- Reconcile must use **guarded writers only** (`defer_run_terminal` /
  `settle_deferred_run` / `record_run_output`, whose UPDATEs are scoped to
  `queued|running` and resolve races through `_stronger_terminal_status`).
  `update_run_status` is unguarded and must not be used for reconcile.

### 1.1 Gap A — a released sink with no terminal emit leaves the run `running` forever

`_execute_agent_run` deliberately opts out of the `finally` terminal write:
`AgentRunExecutionResult(complete_on_return=False)` at `core/scheduled_tasks.py:2596`
and `:2610`, so `should_complete=False` at `:2431` and the `finally` (`:2445-2462`)
writes nothing. The docstring (`:2532-2538`) states the contract: *"the run stays
`running` until that terminal result is emitted."*

There are exactly two places that release the dispatch waiter:

| Releaser | file:line | Emits a terminal result? |
|---|---|---|
| `_stream_chunk`, `completes_turn` + token match | `core/message_dispatcher.py:104-122` | **Yes** — the honest path |
| `Controller.mark_turn_complete` | `core/controller.py:1099-1122` | **No** — only `done_event.set()` |
| `settle_bound_turn_sink` (Running-tab End) | `core/session_turns.py:1836-1860` | No (documented fallback; live backends normally emit first) |

`mark_turn_complete` is called from `_handle_turn`'s `finally` whenever no agent was
dispatched (`core/handlers/message_handler.py:576-585`). `dispatch_turn` then returns
`None` (`core/services/dispatch.py:120-121`), which `_execute_agent_run:2610` reads as
"submitted, terminal will arrive later" — indistinguishable from the honest
fire-and-forget case. Result: `status=running` forever, plus two derived zombies
(`callback_status` stays `pending` because `list_pending_callbacks` requires a terminal
status + `completed_at`, `storage/background.py:964-979`; `agent_sessions.agent_status`
stays `running` until the next controller restart).

**Reachable today, no infrastructure fault needed:** a whitespace-only message.
`_agent_run_message_for_request` rejects only falsy values (`core/scheduled_tasks.py:2410`),
while `_handle_turn` returns early on `not message.strip()` (`core/handlers/message_handler.py:103-106`).
The other no-dispatch early returns (`:130`, `:137`, `:145`) are `is_human`-gated so
they are unreachable for a harness run; the missing-backend path is safe because it
emits a terminal error and returns a truthy string → `failed`. So the general class is
"any current or future no-dispatch early return", and the blank message is today's
concrete instance.

`dispatch_turn`'s concurrent-turn refusal (`core/services/dispatch.py:86-95`) has the
same DB symptom but a **different shape**, and an earlier draft of this plan was wrong
about it: that branch returns `None` *before* `register_turn_sink` (`:107`) ever runs,
so there is no sink to inspect and a naive `settled_by` read yields `None`. It must be
terminalized explicitly — see §3.2/§3.3 and the dedicated test in §5. (Reviewer's
blocking item 1.)

Coverage overlap, stated plainly: B3's orphan sweep (§4) would eventually catch a
blank-message zombie too, because once `_execute_claimed_request` returns the run is in
neither ownership source. Gap A is still worth fixing on its own — it settles instantly
with a precise reason instead of 60–180 s later with `orphaned` — but the two are
deliberately belt-and-braces, not a single point of failure.

### 1.2 Gap B — no periodic staleness reconcile; `queued` never ages at all

The only reconciler is startup-only and touches `running` only. Nothing ages a
`queued` row, so these are permanent until the next restart (and B1/B2 survive
restart because recovery requeues to the same skipped state):

- **B1 — transport never ready.** `_drain_requests` skips a pending run when
  `_transport_ready_for_request` is false (`core/scheduled_tasks.py:2023`) with no age
  limit. A platform later disabled in config, or credentials that never validate,
  strands every run targeting it.
- **B2 — workbench queue hold.** `requeue_on_return` stamps
  `metadata.workbench_queue_holds_run=True` (`:2432`) and `list_pending` filters those
  rows out (`:1045`). Release depends on
  `SessionTurnManager.recover_persisted_agent_run_queue` (`core/session_turns.py:1130`)
  whose five bail-outs (`:1165`, `:1169`, `:1178`, `:1205`, `:1218`) each leave the row
  invisible to the scheduler; `defer_to_scheduler` also chains, so one stuck row can
  freeze later held rows in the same session.
- **B3 — `running` with no owner.** Eviction / silent backend death / a lost receiver
  leaves `running` with nothing awaiting it. PR2 covers the *eviction* path
  (cancel + reconcile at teardown); B3 is the residual "no teardown event ever fired"
  case, which no signal-driven fix can reach.

Explicitly **not** in scope: a turn-duration timeout. `core/services/dispatch.py:66-71`
and `core/session_turns.py:761` state by design that an agent turn may run for hours
and must never be killed on a timer. The sweep below is **evidence-based**, not
timer-based; the only time inputs are grace periods that protect against races.

## 2. Goals / non-goals

Goals:

1. Every `agent_run` reaches a terminal status, or is provably owned by a live
   execution — no third state.
2. Stranded `queued` rows fail honestly with a diagnosable reason instead of
   waiting forever.
3. Zero behaviour change for the honest path (terminal `result` emitted).
4. No new status value, no schema migration, no collision with PR1–PR7.

Non-goals: turn-duration timeout; PR7's scheduled/watch settlement change;
PR2's teardown cancel; PR6's notification ladder (this plan records
`error` + `interrupt_reason` so PR6 can consume it later, and the callback
already ships for free once `status` + `completed_at` land).

## 3. Design — Gap A: settlement honesty

### 3.1 Mark why the sink was released (single source of truth)

The sink dict is already the shared per-turn state. Add one key, written at the
three release sites, using a shared constant module-level enum-ish string set:

- `core/message_dispatcher.py:119` (before `done.set()`) → `sink["settled_by"] = "terminal_result"`
- `core/controller.py:1120` (`mark_turn_complete`, before `done.set()`) →
  `sink.setdefault("settled_by", "no_terminal_result")`
- `core/session_turns.py:settle_bound_turn_sink` → `sink.setdefault("settled_by", "stopped")`

`setdefault` matters: a terminal result that already settled the sink must win over a
later fallback release.

### 3.2 Report it out of `dispatch_turn`

`dispatch_turn` owns the sink lifecycle, so it is the only place that can read
`settled_by` before `pop_turn_sink`. Add a typed outcome without touching the existing
signature (web Chat, IM, and the scheduled path all call `dispatch_turn` today):

```python
@dataclass(frozen=True)
class TurnDispatchOutcome:
    error: Optional[str]
    settled_by: Optional[str]      # None ONLY for a non-streaming caller (on_chunk is None)

async def dispatch_turn_with_outcome(...) -> TurnDispatchOutcome: ...
async def dispatch_turn(...) -> Optional[str]:   # unchanged wrapper
    return (await dispatch_turn_with_outcome(...)).error
```

Every streaming path must yield a non-`None` `settled_by`. Specifically the
concurrent-turn refusal at `:86-95` returns before a sink exists, so it synthesizes
`settled_by="refused_concurrent_turn"`. `settled_by is None` therefore means exactly
one thing — "this caller passed no `on_chunk`" — which `_execute_agent_run` never does.

### 3.3 Terminalize in `_execute_agent_run`

At `core/scheduled_tasks.py:2610`, replace the unconditional
`complete_on_return=False` with:

- `settled_by == "terminal_result"` → `complete_on_return=False` (unchanged; the
  out-of-band writer owns the terminal state). This is the only branch that keeps the
  run open.
- any other `settled_by` (`no_terminal_result` / `stopped` /
  `refused_concurrent_turn`) → settle the run now (§3.3.1) with an i18n'd `error` and
  `metadata.interrupt_reason` set to that same value.
- **Revised during implementation (post-review):** the terminal *status* is no longer
  uniformly `failed`. `SETTLEMENT_TERMINAL_STATUS` (`core/run_settlement.py`) maps
  `stopped` → `canceled` and the rest → `failed`. `stopped` is the one settlement
  carrying explicit user intent (Running-tab End), and `canceled` is already in the
  closed vocabulary for exactly that — a stopped run did not break, it was called off.
  This also disarms the `settle_bound_turn_sink` ordering race (§3.5): whichever way
  it falls, the row reads something true.
- `settled_by is None` → non-streaming caller, unreachable from this path; settle as
  `no_terminal_result` and `logger.warning` rather than leaving the run open. Never
  return `complete_on_return=False` for an unknown settlement.

### 3.3.1 Settle through the guarded writer, not the unguarded `finally`

**O1 resolved (reviewer's blocking item 2):** `update_run_status`
(`storage/background.py:1042-1125`) has no status predicate — its UPDATE is
`where(agent_runs.c.id == run_id)` (`:1120`) — so `_execute_claimed_request`'s
`finally` → `TaskExecutionStore.complete` would clobber a row another actor already
settled `canceled` (a live `vibe runs cancel` racing this settlement). Since PR-A's new
branches are exactly the ones that terminalize on return, they must **not** ride that
chokepoint. Instead `_execute_agent_run` itself calls
`defer_run_terminal` + `settle_deferred_run` (guarded to `queued|running`, races
resolved via `_stronger_terminal_status`, `storage/background.py:110-118`) and returns
`complete_on_return=False` so the `finally` stays a no-op. The `finally` keeps its
current semantics for every other run type — one behaviour change, one code path.

Consequence: a `canceled` row wins, and a concurrent cancel arriving mid-settlement is
a genuine TOCTOU that the test in §5 must reproduce, not a static fixture.

### 3.3.2 Required writer extension

**Reviewer's blocking item 3:** `defer_run_terminal` / `settle_deferred_run`
(`storage/background.py:1392-1519`) write only `result_payload_json` and the terminal
columns — **neither touches `metadata_json`**. Both Gap A and Gap B need
`metadata.interrupt_reason`, so extending those two signatures to accept and *merge*
(never replace) a metadata patch is in-scope implementation work for PR-A, and PR-B
reuses it. Merge semantics must not drop keys other writers own
(`workbench_queue_holds_run`, `coalesced_queue`, `source_kind`, …).

Define the `interrupt_reason` values as shared constants in one module so this branch
(`no_terminal_result`, `stopped`, `refused_concurrent_turn`, `orphaned`,
`transport_unavailable`, `queue_hold_expired`) and the harness plan's (`evicted`,
`restarted`, `lifetime_timeout`) cannot drift apart.

### 3.4 Reject a blank harness message at the two doors

Independently of §3.3 (belt and braces, and it keeps the failure legible):

- `core/scheduled_tasks.py:2410` — `if not message or not message.strip(): raise ValueError(...)`
  → the existing `except Exception` makes it a clean `failed` with a real reason.
- `vibe/cli.py` `cmd_agent_run` — reject a blank `--message` / `--message-file`
  before enqueue, so the CLI fails fast instead of creating a row that must then be
  swept.

**As implemented (PR-A):**

- The CLI door was already closed: `_resolve_message_input` (`vibe/cli.py:1215-1262`)
  strips and rejects both an empty `--message` and an empty `--message-file`, so no
  change was needed there. Verified, not assumed.
- The guard moved one level deeper than planned as well: `TaskExecutionStore.enqueue_agent_run`
  now raises on a blank message, which closes the door for *every* producer rather
  than just the CLI. The only in-tree callers are the CLI and `enqueue_session_callback`,
  which already returns `None` for a blank message, so nothing legitimate is refused.
- `_execute_claimed_request`'s pre-dispatch check became `not message.strip()`, which
  covers rows enqueued before the store-level guard existed.

**Out of PR-A scope (deliberate):** the workbench gate path. When
`_execute_agent_run` hands the turn to `gate.submit_scheduled` (`core/scheduled_tasks.py`
workbench branch) the gate returns `enqueued` / `duplicate` / `ran`, and the `ran`
result never passes through `dispatch_turn`, so there is no `settled_by` to branch on.
Threading a settlement through `SessionTurnGate.submit` → `manager.submit` is a larger
refactor of a second dispatch lane. Gap B's B3 orphan sweep is the backstop for a
stranded row on that lane; §3.4's door checks remove the one reachable instance.

### 3.5 Ordering against a real terminal result (added post-review)

The review flagged the one place where PR-A changes a *consequence* rather than only
fixing a zombie: `settled_by == "stopped"`. `_stop_active_agent`
(`core/services/running_agents.py:983-990`) awaits `handle_stop`, then calls
`settle_bound_turn_sink`. Before PR-A, a terminal result that landed after that point
still won and the row read `succeeded`; PR-A settles the row the moment the stop
releases the waiter, and both writers are scoped to `queued|running`, so whichever
lands first wins permanently.

Resolved by precedence rather than by a timing assumption or a timer (a turn-duration
timeout is explicitly out of this design):

- **Terminal lands first** — `settle_bound_turn_sink` finds `done` already set and
  returns `False` without stamping, so `settled_by` stays `terminal_result` and the
  run settles from its true result. This is the common case and it is now pinned by
  `test_stop_defers_to_a_terminal_result_that_already_landed`.
- **Terminal lands after the stop was acknowledged** — the `canceled` settlement wins
  and `record_run_output`'s terminal write becomes a no-op, *provided the stop's row
  write got there first*. (**Corrected by §5.3 P2**: this bullet originally read as a
  guarantee, but the stop's row write happens later than its in-memory stamp, and a
  result arriving in between used to overwrite the reason and skip the `canceled`
  settlement entirely. The reason is now protected; whether `canceled` reaches the row
  remains first-writer-wins.) The precedence on the reason is deliberate:
  `canceled` is true of a run the user stopped regardless of what the backend was
  about to say, and the late text is still appended to the run's outputs
  (`record_run_output` returns `recorded=True, terminal_transition=False`). Pinned by
  `test_late_terminal_result_cannot_reopen_a_stopped_run` so a future change to either
  writer's guard is a test failure, not a silent flip in who wins.

The docstring on `settle_bound_turn_sink` now states this rule instead of asserting
that live backends always emit before `handle_stop` returns — that was an unenforced
claim about async backend timing, and the settlement layer should not depend on it.

## 4. Design — Gap B: evidence-based staleness sweep

### 4.1 Ownership provider (who is legitimately executing a run right now)

A `running` row is owned if **any** of these holds. Two sources, because the
workbench path never enters `_inflight_executions`:

1. `request.id in ScheduledTaskService._inflight_executions` (the drain path).
2. `SessionTurnManager` live state references the run id — the workbench claim path
   (`claim_queued_runs_for_workbench_in_connection`, `storage/background.py:289-347`)
   claims a `primary_run_id` that is executed through `flush_queue`, not the drain.
   **Reviewer's blocking item 5 — derivation must be explicit:** `Turn`
   (`core/session_turns.py:562-566`) carries no run-id field, so
   `SessionTurnManager.owned_agent_run_ids() -> set[str]` reads
   `Turn.context.platform_specific["task_execution_id"]` for every live `in_flight`
   turn (the same key `_turn_sink_identity` uses, `:1715-1717`), and unions the
   `coalesced_queue.execution_ids` list when present — a coalesced turn owns **all**
   ids it is settling, not just the primary, or the sweep would fail the siblings out
   from under a live flush.

   Verified by review: source 1 is race-free — `_spawn_execution`
   (`core/scheduled_tasks.py:2336-2343`) registers the task in the same synchronous
   block as `claim()`, with no `await` between the DB write and the in-memory insert,
   so `running` can never be visible without its owner.

**A missing or failing provider must fail closed** (treat as owned → do not sweep).
Sweeping a live run would double-settle and, worse, could look like a false failure
notification to the user.

**As built (deviation 5):** `_owned_agent_run_ids()` unions `_inflight_executions` with
`SessionTurnManager.owned_agent_run_ids()`, and the latter reads **both**
`in_flight` turns *and* the live `active_turn_sinks` — a sink can be registered for a
run before/after its `Turn` is in `in_flight`, and reading only `in_flight` leaves that
window sweepable (mutation M2 confirms: dropping the `active_turn_sinks` union kills
`test_sweep_skips_running_run_owned_by_workbench_turn`). Fail-closed is implemented as
`raise RuntimeError` from the provider lookup plus a `try/except → return` in
`_sweep_stale_runs`, warning once rather than once per interval; the store method
cannot distinguish "nothing is owned" from "I could not look", so the decision has to
live in the caller.

### 4.2 What the sweep does

Runs from `ScheduledTaskService._watch_store` (the existing 2 s tick,
`core/scheduled_tasks.py:1884`) at most every `sweep_interval` (default 60 s),
only when `_owns_service_instance()` — same gate as every other drain. Exclusions,
mirroring `recover_processing_runs`: `run_type='watch_runtime'`, and any row carrying
`result_payload_json.deferred_terminal_status` (that is PR2/Activity territory).

| Class | Precondition | Action |
|---|---|---|
| B3 orphaned `running` | `run_type='agent_run'`, not owned per §4.1, `started_at` older than `orphan_grace` (default 120 s) | `failed`, `interrupt_reason="orphaned"` |
| B1 stranded `queued` | `created_at` older than `queued_ttl` (default 1800 s) **and** the drain's own skip reason is transport-unavailable | `failed`, `interrupt_reason="transport_unavailable"` |
| B2 held `queued` | `metadata.workbench_queue_holds_run` **and** `updated_at` older than `hold_ttl` (default 3600 s) | `failed`, `interrupt_reason="queue_hold_expired"` |

Ownership per §4.1 exempts a row from **all three** classes, not just B3 — see §5.5: a
coalesced turn's owned siblings stay `queued` on purpose, so the queue TTLs have to
respect a live owner too.

Terminalizing the row is not the whole reclamation. A swept run's persisted Workbench
queue segment (`messages` rows keyed `agent_run:<run_id>`) is nobody else's to reclaim
once the row leaves `queued`, so the sweep also retires each expired run's own segment
and republishes `queue.updated` — see §5.6.

Notes:

- B1's precondition should be **recorded, not re-derived**: have
  `_drain_requests` stamp `metadata.last_skip_reason` (+ a coarse `last_skip_at`) on the
  skip branches (`:2023`, `:2026`, capacity `:2017`), so the sweep never guesses and
  the row itself explains why it sat. A row skipped only for capacity or a session lock
  is **never** swept — it is making progress. (**Superseded in part by §5.3 P1**: the
  recorded reason is necessary but not sufficient, because the drain stops at its
  concurrency cap and stops refreshing the stamp. A live deliverability check is now
  required alongside it.)

  **Reviewer's blocking item 4 — this stamp must not self-trigger the drain.**
  `_watch_store`'s gate is `maybe_reload()` → `SqliteInvalidationProbe.has_external_write()`
  (`storage/db.py:82-108`), which bumps on *any* write to the DB file including our own.
  A naive stamp on every skip iteration therefore becomes a permanent 2 s
  write → reload → re-drain → write loop for as long as a transport is down, plus a
  `runs.updated` event each cycle. Required design: write **only when the stored reason
  changes** (transition-triggered, not tick-triggered), keep `last_skip_at` bucketed to
  a coarse interval (e.g. the sweep interval) so a steady state produces no write at
  all, and keep the stamp out of `run_update_event_transaction` so it emits no SSE.
  The §5 "no hot loop" test guards this.
- B2's TTL is deliberately longer than B1's and its precondition is `updated_at`, so a
  session actively recovering its queue keeps the row alive.
- The `agent_run`-only restriction on B3 keeps this plan disjoint from PR7 (which
  changes when `scheduled`/`watch` rows settle). Widen it only after PR7 lands.

**Correction (as-built) — what B3's "orphan" actually is.** This section read as if B3
were mainly about surviving a restart. It is not: `ScheduledTaskService.__init__` calls
`request_store.recover_processing()` → `recover_processing_runs()`, which **requeues
every `running` row** (bar `watch_runtime` and deferred ones) at service construction.
So a restart's in-flight runs are *retried*, not swept, and B3 is about owners lost
**within a live process** — a turn that returned without settling, a `create_task` that
never attached its done-callback, a drain task that died between claim and settle. This
also has a hard consequence for the tests: a staged `running` fixture must be written
*after* the service is constructed, or recovery flips it to `queued` first (this cost
nine failing tests before it was diagnosed).

**Correction (as-built) — the exclusions are defended twice.** Mutation testing showed
the `watch_runtime` / deferred exclusions cannot be killed one at a time:
`watch_runtime` is filtered out by the SELECT *and* excluded by B3's
`run_type == "agent_run"` restriction, and a deferred row is skipped by the candidate
loop *and* refused by `settle_run_terminal`'s own deferred guard. Removing both
`watch_runtime` guards kills the test; removing only the deferred candidate check does
not. Kept as-is — this is defence-in-depth, not dead code — and recorded in the test
docstring so nobody later "simplifies" a guard on the strength of a green suite.

### 4.3 Writers and side effects

One new store method, guarded, alongside `recover_processing_runs`:

```python
def sweep_stale_runs(self, *, now, orphan_grace, queued_ttl, hold_ttl,
                     owned_run_ids: set[str]) -> list[SweptRun]
```

It selects candidates, then terminalizes each through the guarded path
(`defer_run_terminal` + `settle_deferred_run`, or a single UPDATE scoped to
`queued|running` honoring `_stronger_terminal_status`), writing `status`,
`completed_at`, `error`, `metadata.interrupt_reason`, and emitting the deferred
`runs.updated` event exactly like `recover_processing_runs:1588`. Because
`list_pending_callbacks` only needs a terminal status + `completed_at`, **the
user-facing callback ships for free** — no new delivery plumbing in this plan.

Also release the in-memory wedge when a swept run holds one: if a swept row's lock key
is still in `_inflight_sessions` while nothing owns the run, discard it. Without this,
the DB is honest but the session stays undispatchable (the same wedge PR2 documents).

**Correction (as-built, deviation 7):** rebuilding the lock key from a `SweptRun` is
unsafe. A lock key is per-*conversation*, not per-run, so a swept run's identity can
resolve to a key a *different*, live execution currently holds, and freeing it would
let two turns run concurrently in one session. As built, `_spawn_execution` records
`_session_lock_owners[lock_key] = request.id` next to the `_inflight_sessions` insert,
and `_release_leaked_session_locks()` frees only keys whose recorded owner is no longer
in `_inflight_executions`. That is provably one-directional: a lock held by a live task
can never be freed. It also runs *unconditionally* after each sweep, not only when a
row was swept, because the wedge and the stale row are independent failures and either
can outlive the other.

### 4.4 Config

New keys under runtime config in `config/v2_config.py`, each with the defaults above
and `0` meaning "disabled":
`harness_run_orphan_grace_seconds`, `harness_run_queued_ttl_seconds`,
`harness_run_hold_ttl_seconds`, `harness_run_sweep_interval_seconds`.
No UI surface in this plan (defaults are the product decision); document in
`docs/`. All user-visible error copy goes through `vibe/i18n/` — note
`core/scheduled_tasks.py` imports no i18n today, which the approved plan already
flags as a violation at this exact seam.

As built, with `DEFAULT_HARNESS_RUN_*` constants exported from `config/v2_config.py`
and read through `_runtime_seconds()`, which falls back to the default on a missing or
non-integer value rather than letting a bad config crash the tick:

| Key | Default | `0` means |
| --- | --- | --- |
| `harness_run_sweep_interval_seconds` | 60 | sweep disabled entirely |
| `harness_run_orphan_grace_seconds` | 120 | never sweep orphaned `running` rows |
| `harness_run_queued_ttl_seconds` | 1800 | never sweep transport-stranded `queued` rows |
| `harness_run_hold_ttl_seconds` | 3600 | never expire a workbench queue hold |

The i18n seam resolved as `SWEEP_I18N_KEYS` in `core/run_settlement.py`, next to
`SETTLEMENT_I18N_KEYS`, mapping each `SWEEP_REASON_*` to
`harness.run.interrupted.<reason>`. The reason strings are spelled as **literals**
there rather than imported from `storage.background`: `core/run_settlement.py` is
deliberately dependency-free so the dispatch layer can import it without pulling in
SQLAlchemy, and importing `core` from `storage` would invert the layering. The drift
risk that creates is closed by a test
(`test_sweep_reason_i18n_map_covers_every_store_sweep_reason`) asserting the map's key
set equals the store's constants.

## 5. Test plan (hermetic; `tests/conftest.py:53-72` autouse isolation, no `uses_real_paths`)

`tests/test_scheduled_tasks.py` (the natural home; `test_agent_run_stays_running_until_terminal_result:2054`
is the template for the sink-driven cases):

- `test_agent_run_with_blank_message_fails_instead_of_hanging` — the §1.1 reproducer.
- `test_agent_run_settles_failed_when_sink_released_without_terminal_result` — Gap A
  proper: stub `_handle_turn` to call `mark_turn_complete` only.
- `test_agent_run_settles_when_dispatch_refuses_a_concurrent_turn` — **load-bearing
  (reviewer's blocking item 6)**: drives `core/services/dispatch.py:86-95` with a sink
  already registered for the session key and asserts the row terminalizes instead of
  staying `running`. This is the case an earlier draft of this plan got wrong.
- `test_agent_run_stays_running_until_terminal_result` — must still pass unchanged
  (regression guard for goal 3), extended to assert `settled_by == "terminal_result"` is
  actually stamped, so the test fails if the stamp is dropped.
- `test_agent_run_cancel_racing_settlement_keeps_canceled` — §3.3.1's TOCTOU: flip
  `cancel_requested` / settle the row `canceled` **between** `_execute_agent_run`
  computing its outcome and the settlement write, and assert the row stays `canceled`.
  A static already-canceled fixture would pass even with the unguarded writer, so it
  must be a real interleaving.
- `test_sweep_terminalizes_orphaned_running_run`
- `test_sweep_skips_running_run_owned_by_inflight_execution`
- `test_sweep_skips_running_run_owned_by_workbench_queue` — the §4.1 source-2 trap.
- `test_sweep_fails_closed_when_ownership_provider_raises`
- `test_sweep_terminalizes_queued_run_skipped_for_transport` /
  `test_sweep_ignores_queued_run_skipped_only_for_capacity`
- `test_sweep_releases_leaked_session_lock`
- `test_sweep_respects_deferred_terminal_and_watch_runtime_exclusions`
- `test_stranded_queued_run_does_not_trigger_repeated_metadata_writes` — **load-bearing
  (reviewer's blocking item 6)**: across several `_watch_store` ticks with a transport
  permanently unready, assert the row's `updated_at` / write count stays flat, guarding
  §4.2's write-amplification loop.
- `test_sweep_retries_queue_recovery_before_expiring_a_hold` — O4's resolution.

`tests/test_inbox_events.py` — a swept row surfaces in `list_pending_callbacks`
(proves the free-callback path) and the `runs.updated` event fires.

`tests/test_message_dispatcher_scheduled.py` — `settled_by="terminal_result"` is
stamped on the honest path.

Plus the en/zh i18n key-parity test the approved plan already wants (there is none
today), and `ruff check` on changed files before push. No UI change → no `npm run build`.

### 5.1 What PR-A actually landed

Delivered (all verified to FAIL with the fix reverted, so none is a tautology):

- `tests/test_scheduled_tasks.py`
  - `test_agent_run_settles_failed_when_sink_released_without_terminal_result`
  - `test_agent_run_settles_when_dispatch_refuses_a_concurrent_turn` (load-bearing)
  - `test_agent_run_cancel_racing_settlement_keeps_canceled` (load-bearing TOCTOU;
    the cancel is applied *inside* the patched settlement call, so the interleaving
    is real — with an unguarded `update_run_status` the row comes back `failed`)
  - `test_agent_run_cancel_requested_settles_canceled_not_failed` — the in-transaction
    `cancel_requested` → `canceled` mapping
  - `test_agent_run_with_blank_message_fails_instead_of_hanging` — covers both the
    store-level door and a legacy pre-guard row
  - `test_agent_run_stopped_by_user_settles_canceled` — §3.5's status mapping, driven
    through the *real* `settle_bound_turn_sink` (`_StopSinkSettler` borrows the actual
    methods) so the stamp site and its guards are under test, not a hand-written string
  - `test_stop_defers_to_a_terminal_result_that_already_landed` — the safe half of the
    stop race; fails if the `done.is_set()` bail-out is removed
  - `test_late_terminal_result_cannot_reopen_a_stopped_run` — the lossy half, pinned as
    deliberate precedence; fails if `record_run_output`'s terminal guard is widened
  - `test_drain_requests_agent_run_passes_agent_name` — **updated**: it used to assert
    the run stays in `processing`, which encoded the zombie for the legacy file store.
    It now asserts the run is completed with the settlement reason.
- `tests/test_core_services_dispatch.py` — four `TurnDispatchOutcome` cases, including
  that `settled_by is None` means *only* "non-streaming caller".
- `tests/test_dispatcher_stream_chunk.py` — the `terminal_result` stamp is written by a
  result emit, not by a notify emit, and not by a stale turn's late result.
- `tests/test_i18n_backend_keys.py` (new file) — en/zh key parity, no blank strings,
  and every `SETTLEMENT_I18N_KEYS` entry resolves in every supported language.

Deferred to PR-B (they test the sweep, which PR-B introduces): every `test_sweep_*`
case and `test_stranded_queued_run_does_not_trigger_repeated_metadata_writes`.

Mutation evidence for the §3.5 additions (each mutation reverted after the run):

| Mutation | Tests killed |
| --- | --- |
| `SETTLEMENT_TERMINAL_STATUS[stopped]` → `failed` | `..._stopped_by_user_settles_canceled`, `..._late_terminal_result_cannot_reopen...` |
| drop the `done.is_set()` bail-out in `settle_bound_turn_sink` | `..._stop_defers_to_a_terminal_result_that_already_landed` |
| widen `record_run_output`'s terminal guard to include `canceled` | `..._late_terminal_result_cannot_reopen_a_stopped_run` |

Also verified green (no behavior regressions): `test_message_dispatcher_scheduled.py`,
`test_controller_dispatch_loop.py`, `test_internal_server.py`, `test_inbox_events.py`,
`test_cli_agent_run_schema.py`, `test_vault_request_callbacks.py`,
`test_cli_task_command.py`, `test_session_activities.py`,
`test_background_work_banner.py`, `test_harness_payload_enrichment.py`,
`test_sqlite_state_migration.py`, `test_cli_pagination.py`, `test_ui_server_fastapi.py`.
Pre-existing unrelated flake: `test_request_store_file_backend_reload_detects_queue_changes`
(mtime granularity; fails on the untouched base too).

### 5.2 What PR-B actually landed

18 new cases in `tests/test_scheduled_tasks.py`, all hermetic, plus the store/i18n
parity test. Every staged `running`/`queued` fixture is written **after** the service is
constructed (see §4.2's recovery correction) and forced into the past with a raw
`update(agent_runs)` helper rather than by moving a clock.

| Test | What it pins |
| --- | --- |
| `test_sweep_terminalizes_orphaned_running_run` | B3 happy path: `failed` + `interrupt_reason="orphaned"` |
| `test_sweep_respects_the_orphan_grace_period` | a young unowned row is left alone |
| `test_sweep_skips_running_run_owned_by_inflight_execution` | ownership source 1 |
| `test_sweep_skips_running_run_owned_by_workbench_turn` | ownership source 2, through the **real** `SessionTurnManager.register_turn_sink` with a `MessageContext` carrying `task_execution_id` + `coalesced_queue.execution_ids` |
| `test_sweep_fails_closed_when_ownership_is_unknown[provider-missing\|provider-raises]` | §4.1's fail-closed rule, both ways it can break |
| `test_sweep_terminalizes_queued_run_stranded_by_a_dead_transport` | B1 happy path |
| `test_sweep_leaves_a_queued_run_whose_session_is_merely_busy` | drives the **real** `_drain_requests` and asserts the stamped reason is `session_busy`, so a busy session is never swept |
| `test_sweep_ignores_queued_run_skipped_only_for_capacity` | an unstamped row is unsweepable |
| `test_sweep_expires_a_workbench_queue_hold_only_after_its_ttl` | B2 both sides of the TTL |
| `test_sweep_leaves_watch_runtime_and_deferred_rows_alone` | the exclusions (defended twice — see §4.2) |
| `test_sweep_releases_a_leaked_session_lock` | the wedge release |
| `test_execution_completion_does_not_steal_a_later_lock_owner` | deviation 7's safety direction |
| `test_stranded_queued_run_does_not_trigger_repeated_metadata_writes` | reviewer item 4: `wrote == [True, False, False, False]` across four ticks and `updated_at` unchanged |
| `test_swept_run_notifies_the_session_that_launched_it` | the free callback path |
| `test_sweep_publishes_a_run_update_event` | `runs.updated` fires (subscription unsubscribed in `finally` — `subscribe_callback` is persistent, not one-shot) |
| `test_sweep_is_rate_limited_to_the_configured_interval` | rewinds `_last_sweep_at`, no clock patching |
| `test_sweep_is_disabled_by_a_zero_interval` | the kill switch |

Mutation evidence — 12 of 13 targeted mutations killed, each reverted afterwards:

| Mutation | Verdict |
| --- | --- |
| M1 sweep ignores `owned_run_ids` | KILLED (both ownership tests) |
| M2 ownership omits live `active_turn_sinks` | KILLED |
| M3 unknown ownership read as empty instead of raising | KILLED |
| M4 sweep `queued` rows without a recorded skip reason | KILLED (2 tests) |
| M5 drain does not stamp `session_busy` | KILLED |
| M6 skip reason written every pass | KILLED |
| M7 skip stamp bumps `updated_at` | KILLED |
| M8 wedge release ignores live owners | KILLED |
| M9 completion steals a later lock owner | KILLED |
| M10 no sweep rate limit | KILLED |
| M11 drop one `watch_runtime`/deferred guard | **SURVIVED** — both classes are guarded twice; M11c (remove the SELECT filter *and* the `agent_run` restriction) KILLS. Documented rather than forced. |
| M12 no orphan grace | KILLED |
| M14 transport class ignores `deliverable_run_ids` (review round 1) | KILLED |
| M15 result overwrites a recorded `stopped` (review round 1) | KILLED |
| M16 unknown deliverability does not disable the class (review round 1) | KILLED |

Deviations from §4 as written, all deliberate: (1) `record_run_skip_reason` writes only
on reason *change*, with no `last_skip_at` bucketing — a transition-only stamp already
produces zero steady-state writes, so the bucket was unnecessary machinery; (2)
`session_busy` is stamped too, so it can overwrite a stale `transport_unavailable` and
un-sweep a row whose transport came back; (3) capacity skips are deliberately left
unstamped — the drain `break`s without examining the rest of the queue, and an unstamped
row is never sweepable, so silence is the safe encoding; (4) each row is terminalized
through `settle_run_terminal` rather than `defer_run_terminal` + `settle_deferred_run`,
inheriting exactly the same guards with one write instead of two; (5) ownership unions
live sinks (§4.1); (6) B3 stays `agent_run`-only per O5; (7) the owner-map wedge release
(§4.3); (8) no pre-expiry queue-recovery attempt (§7 O4).

Validation: `uvx ruff check core/ storage/ config/ tests/` clean; 211 passed across
`test_scheduled_tasks.py`, `test_i18n_backend_keys.py`, `test_inbox_events.py`,
`test_core_services_dispatch.py`, `test_controller_dispatch_loop.py`,
`test_message_dispatcher_scheduled.py`. One unrelated pre-existing flake,
`test_request_store_file_backend_reload_detects_queue_changes` — reproduced from a clean
`git archive` of base master `5921ad39` (2/3 failures) as well as of this HEAD (3/3), so
it is not caused here. Cause: the legacy file store's `_path_signature` relies on
directory `st_mtime_ns`, which the kernel's coarse timestamp clock can report identically
for two writes in the same jiffy. Fix candidate for a separate PR: fold the sorted
directory entry names into the signature instead of trusting mtime alone.

### 5.3 Review round 1 (avibe-bot/avibe#1005) — two real defects

Both findings were reproduced against the code before being fixed, and each fix is
pinned by a test that fails when the fix is reverted.

**P1 — stale transport evidence could fail a deliverable run (HFR-021, HFR-022).**
§4.2 said the recorded skip reason is read off the row and never re-derived, so a
transport that came back cannot erase the fact that a run missed it. That reasoning only
holds for rows the drain still visits. The drain `break`s at
`_MAX_CONCURRENT_EXECUTIONS` without examining the remaining queue (deviation 3's own
premise), so a row below the cut keeps a `transport_unavailable` stamp indefinitely —
including long after its platform reconnected — and would be failed for a transport
outage that is over. The evidence now has two independent halves, both required: the
recorded reason **and** a live `deliverable_run_ids` set, computed per sweep from
`request_store.list_pending()` × `_transport_ready_for_request`. Any queued run whose
transport answers ready is exempt, so a run waiting only for a free slot is never swept.
If that live half cannot be computed the class disables itself for the tick
(`queued_ttl_seconds = 0`) — the same fail-closed posture as unknown ownership, since an
unprovable claim must not terminalize a run. The other two classes keep working.

**P2 — a terminal result could overwrite an acknowledged stop's reason (HFR-023).**
§3.5 claimed the stop/result precedence was settled by both writers being scoped to
`queued|running`. It was not, one layer up: the stop stamps `settled_by="stopped"` in
memory and releases the waiter immediately, while the row is written later when
`_execute_agent_run` reads the stamp back. A terminal result arriving inside that window
overwrote the reason unconditionally, so the guarded `canceled` settlement was skipped
and the recorded outcome depended on which coroutine resumed first — while the user had
already been told the run was stopped. `_stream_chunk` now refuses to overwrite a
recorded `stopped`. Deliberately *not* fixed by widening the guarded writer to override
an existing terminal status: that would break the one guarantee every settlement path
relies on. Whether `canceled` reaches the row therefore stays ordinary
first-writer-wins, and `settle_bound_turn_sink`'s docstring now says so instead of
claiming a precedence the code does not provide.

### 5.4 Review round 2 (avibe-bot/avibe#1005) — the TTL was measured from the wrong clock

**P1 — `queued_ttl` aged from `created_at` instead of from the outage (HFR-024, HFR-025).**
Round 1 fixed *which* rows the transport class may consider; this is *when* it may act.
A run can sit in `queued` far longer than the TTL for reasons that are progress —
capacity, a busy session. Aging from `created_at` meant that as soon as such a run's
transport blinked, the row satisfied the TTL immediately and was failed on the next
tick, consuming none of the configured 30-minute reconnect window. The TTL is a
reconnect window, so it now ages from `metadata.last_skip_at`. That timestamp is already
exactly right for the job: `record_run_skip_reason` is transition-triggered (deviation 1),
so `last_skip_at` means "when this reason started", not "when we last looked".

A reason with no parseable timestamp is treated as unrecognized evidence and never
swept — `_older_than` already fails closed on an undateable value, and the reason and
timestamp are written in one statement, so one without the other did not come from that
writer. A `created_at` fallback was considered and rejected: it would silently
reintroduce this exact bug for those rows, which is what HFR-025 now pins.

| Mutation | Verdict |
| --- | --- |
| M17 fall back to `created_at` when `last_skip_at` is absent | KILLED (HFR-025) |
| M18 age from `created_at` outright (the reported bug) | KILLED (HFR-024 + HFR-025) |

### 5.5 Review round 3 (avibe-bot/avibe#1005) — ownership is not a `running`-only exemption

**P1 — a live coalesced turn's own `queued` siblings were sweepable (HFR-026).**
The ownership check sat inside the `running` branch, which reads as correct only if you
assume an owned run is always `running`. It is not: a coalesced Workbench turn claims its
secondary runs and deliberately leaves them `queued` with `workbench_queue_holds_run`
while the primary settles them (`storage/background.py:384`, `:478`), and
`owned_agent_run_ids()` reports **every** id such a turn is settling
(`core/session_turns.py:635`). So a turn that outlived `hold_ttl` (default 1 h) had its
own live siblings failed underneath it — a turn-duration timeout by the back door, which
this design explicitly does not have. The same applies to any queued follow-up waiting
behind a legitimate multi-hour turn.

The fix hoists the check to the top of the candidate loop as a single `continue`: a live
owner outranks every class, whatever the row's status. The branch-level
`run_id not in owned_run_ids` was **removed** rather than left in place, so there is
exactly one load-bearing guard — a duplicated check would produce another surviving
mutation like the documented M11 and hide a future regression.

| Mutation | Verdict |
| --- | --- |
| M19 restrict the hoisted guard back to `status == "running"` (the reported bug) | KILLED (HFR-026) |

### 5.6 Review round 4 (avibe-bot/avibe#1005) — the gate lane had no settler, and a swept hold left its queue behind

**P1 — an `avibe`-targeted run's turn could end without anyone settling the row (HFR-027).**
§3.3 settles on the *drain* lane, which is correct for every backend that completes inside
`_execute_agent_run`. An `avibe` target does not: it hands the prompt to
`session_turn_gate.submit_scheduled` and returns `complete_on_return=False`
(`core/scheduled_tasks.py:2891`, `:3137`) while the turn is still live, deliberately leaving
the row to the *turn* lane. But `SessionTurnManager._run` called the thin `dispatch_turn`
compat wrapper, which returns only `outcome.error` and discards `settled_by`. So a turn
released without a terminal result — stopped, evicted, refused, or raising — left its run
`running` until the B3 orphan grace expired, and a coalesced turn stranded its claimed
siblings too. This is the same defect as §3.1–3.3, one lane over.

Fix: `_run` now calls `dispatch_turn_with_outcome`, records `settled_by`
(`SETTLED_BY_STOPPED` on `CancelledError`, `SETTLED_BY_NO_TERMINAL_RESULT` on an
exception), and in its `finally` calls the new `_settle_turn_owned_agent_runs`, which
settles **every** id in `_agent_run_ids_from_spec` — primary plus coalesced — through
`ScheduledTaskService.settle_agent_runs_without_result`. That new entry point reuses the
existing *guarded* first-writer-wins writer and the drain lane's i18n text, so the two
lanes cannot fight: a real terminal result wins if it landed first, and this write
degrades to a no-op. Ordering matters twice — after the failure emit (so the honest
outbound terminal writes first, per §3.5) and before the queue flush (so the next turn
never starts while a run this turn owned is still `running`). `settled_by is None` means
no sink was bound at all, and a `SETTLED_BY_TERMINAL_RESULT` means a result did arrive;
both return without guessing.

**P2 — expiring a queue hold reclaimed the row but not its persisted queue (HFR-028).**
A `workbench_queue_holds_run` run also owns a persisted `messages` row of type
`QUEUED_TYPE` keyed `agent_run:<run_id>`. `recover_persisted_agent_run_queue` ignores
references whose run is no longer `queued`, so once B2 terminalized the row nothing could
ever reclaim that segment: the Session kept showing stale pending input until an
unrelated send forced a flush. `SweptRun` already carried the `session_id` for exactly
this and never used it. `_sweep_stale_runs` now calls `_retire_swept_queue_segments`,
which per swept run calls the existing `_retire_stale_agent_run_queue_rows(session_id=…,
execution_ids=[run_id])` and publishes one `queue.updated` per touched session.
Retirement is scoped to each run's **own** native id, so it can never touch a live
sibling's row, and a per-run failure is logged and skipped rather than aborting the sweep.

| Mutation | Verdict |
| --- | --- |
| M20 drop the turn-lane settlement (keep the drain lane only — the reported P1) | KILLED (HFR-027) |
| M21 skip `_retire_swept_queue_segments` after a sweep (the reported P2) | KILLED (HFR-028) |

Collateral: seven pre-existing doubles across `tests/test_internal_server.py`,
`tests/test_scheduled_tasks.py`, and `tests/test_controller_agent_status.py` patched
`session_turns.dispatch_turn` and now patch `dispatch_turn_with_outcome`, returning a
`TurnDispatchOutcome`. `internal_server.dispatch_turn` is intentionally untouched — that
patch targets the legacy streaming `/internal/dispatch` endpoint's own symbol.

### 5.7 Review round 5 (avibe-bot/avibe#1005) — a cancelled task is not always a user stop

**P1 — a backend runtime refresh was reported as if the user pressed Stop (HFR-029).**
§5.6 mapped every `CancelledError` in `_run` to `SETTLED_BY_STOPPED`, on the reasoning
that only a stop path cancels a turn. True, but "stop path" quietly included
`release_for_backend_refresh` (`core/session_turns.py:1487`), which cancels every
in-flight turn of a backend whose cached process state is about to disappear — exactly
what the rolling reconciliation after an `agents.*` save does (`core/backend_restart.py:102`,
`modules/agents/codex/agent.py:396`). So a scheduled run interrupted by routine
configuration work settled `canceled` with the user-stop text: the callback told the user
they had stopped a run they never touched, and the failure accounting saw deliberate
intent where there was an infrastructure fault.

A cancelled task carries no cause, and only the canceller knows it. `Turn` gains
`cancel_settled_by`, set **before** `task.cancel()`, and `_run` resolves the reason in its
`finally` off the Turn it pops — the same lifetime trick the flush intents already use, so
it retires with the turn instead of leaking in a parallel set. The `except
asyncio.CancelledError` block no longer decides anything. A bare cancellation still reads
as `stopped`, because `cancel` and `send_now` are the paths that legitimately have no more
specific reason.

New settlement `SETTLED_BY_BACKEND_REFRESH = "backend_refresh"` → `failed` (an
infrastructure fault with no user intent, so it stays visible to a failure counter) with
its own `harness.run.interrupted.backendRefresh` text in `en`/`zh`. It is deliberately
**not** spelled `restarted`: that value is reserved by `harness-run-reliability.md` for
the *service* restarting around a run, which `recover_processing_runs` retries, whereas
this interrupts a live turn inside a healthy process and does not.

| Mutation | Verdict |
| --- | --- |
| M22 collapse the cancellation attribution back to `SETTLED_BY_STOPPED` (the reported bug) | KILLED (HFR-029) |

### 5.8 Review round 6 (avibe-bot/avibe#1005) — "the turn ended" is not "the Run ended"

**P1 — a run another owner was still retrying got failed (HFR-030/031/032).**
`_settle_activity_turn_after_delivery_failure` (`modules/agents/claude_agent.py:1898`)
closes its origin turn with a silent terminal `result` carrying
`MessageOutput(completes_turn=True, completes_run=False)`: the completion could not be
persisted or delivered, so the **REQUEUED Activity keeps the run** and retries it. That
emit still sets `mutates_turn_lifecycle`, so the release ran through
`_signal_turn_complete` → `Controller.mark_turn_complete`, whose own default reason is the
no-dispatch case (`no_terminal_result`). Both settlement lanes then read a settlement that
means "no result is coming" and terminalized a **live, Activity-owned** run `failed`,
firing its terminal callback before the retry ever ran.

Two halves, because either alone leaves the hole open:

1. The release site supplies the reason. `mark_turn_complete` takes
   `settled_by=` (defaulting to `no_terminal_result` for genuine no-dispatch callers) and
   `_signal_turn_complete` — every one of whose four call sites is a terminal-result emit —
   defaults to `SETTLED_BY_TERMINAL_RESULT`, resolving per-emit through
   `_turn_release_settlement(output_semantics)`: `settles_run` → `terminal_result`,
   otherwise the new `SETTLED_BY_TURN_ONLY_RESULT`. A `TypeError` fallback keeps older
   controllers/test doubles releasing the waiter.
2. Both lanes flip from deny-list to allow-list. `core/scheduled_tasks.py` and
   `core/session_turns.py` now require membership in `SETTLEMENTS_WITHOUT_RESULT` before
   touching a row, instead of "anything but `terminal_result` is a zombie". So the next
   "turn ended, run lives on" reason cannot repeat this failure by omission.

`SETTLED_BY_TURN_ONLY_RESULT = "turn_only_result"` is deliberately absent from
`SETTLEMENTS_WITHOUT_RESULT`, `SETTLEMENT_I18N_KEYS`, and `SETTLEMENT_TERMINAL_STATUS`: like
`terminal_result` it never reaches a row, so it needs no terminal status and no
user-visible text.

| Mutation | Verdict |
| --- | --- |
| M23 drop the `settled_by` propagation in `_signal_turn_complete` (the reported bug) | KILLED (HFR-030) |
| M24 turn lane back to `settled_by == terminal_result` deny-list | KILLED (HFR-031) |
| M25 drain lane back to `settled_by == terminal_result` deny-list | KILLED (HFR-032) |

### 5.9 Review round 7 (avibe-bot/avibe#1005) — a TTL must measure one outage, and the gate's parked rows have no owner

Two P2 findings on `storage/background.py`, both real, both the same underlying mistake:
the sweep was reasoning about *live* facts from *stale* evidence.

**P2a — a recovered transport made the next outage's TTL retroactive (HFR-033).**
`metadata.last_skip_reason = transport_unavailable` is stamped once, on transition
(§4.3, deliberately, to avoid the write → invalidation-probe → reload → re-drain loop).
Nothing retires it. So: stamped at `t0`; transport recovers; the drain that would
re-evaluate the row never reaches it (already at `_MAX_CONCURRENT_EXECUTIONS`), so no
rewrite happens; the sweep exempts the row because `deliverable_run_ids` now contains it.
Transport drops again at `t2 > t0 + queued_ttl_seconds`, deliverability goes away, and the
row is aged from `t0` — swept on the very first tick, with none of the reconnect window
the TTL exists to grant.

The fix retires the evidence at the only moment both halves are visible at once: the sweep
already holds "the row remembers an outage" and "the row is deliverable right now".
`_clear_transport_skip_evidence` pops `last_skip_reason`/`last_skip_at` for exactly those
rows, re-reading each row under the write to skip anything that changed underneath, and
writes **only** `metadata_json` — never `updated_at`, because the hold class's clock reads
that column. It is transition-triggered in the same sense as the stamp: once the evidence
is gone there is nothing left to clear, so a healthy queue costs zero repeat writes.

**P2b — a hold parked behind a live turn was reported by nobody (HFR-034, HFR-035).**
`submit_scheduled` returning `enqueued` requeues the row with
`workbench_queue_holds_run = True`; the gate will flush it when the current turn ends.
But `owned_agent_run_ids()` walks live turn *contexts* and yields only the ids those turns
are executing themselves — a follower the gate parked is owned by no one. So a turn that
legitimately runs longer than `harness_run_hold_ttl_seconds` (default 3600) had its own
queued successor failed underneath it, and the user got an interruption notice for work
that was still correctly waiting.

The hold class therefore needs a third caller-supplied live fact, alongside
`owned_run_ids` and `deliverable_run_ids`: `busy_session_ids`, at **session** granularity,
because that is the granularity the gate occupies. `SessionTurnManager.busy_session_ids()`
reads `in_flight`; `_busy_session_ids()` raises rather than returning a plausible empty
set when the provider is missing, and `_sweep_stale_runs` catches that by failing closed —
`hold_ttl_seconds = 0` disables the class entirely, per the §4.2 posture that an unknown
live fact must never be read as "nothing is live".

| Mutation | Verdict |
| --- | --- |
| M26 drop the recovered-evidence retirement (finding P2a) | KILLED (HFR-033) |
| M27 drop the `busy_session_ids` exemption on the hold branch (finding P2b) | KILLED (HFR-034) |
| M28 drop the caller's fail-closed `hold_ttl_seconds = 0` | KILLED (HFR-035) |

### 5.10 Review round 8 (avibe-bot/avibe#1005) — a stop's empty result is not a success

**P1 — every normally-stopped run was reported as `succeeded` (HFR-037).**
§5.7 gave a user stop the `canceled` terminal it deserves, and recorded in
`settle_bound_turn_sink` that if the backend's own terminal result landed first the row
would keep `succeeded` — judged benign, because either reading is "true of a run the
user stopped". That was wrong twice over.

First, it is not a race. `SessionTurnManager.cancel` awaits `handle_stop`, every backend
answers an acknowledged stop by emitting an empty silent `result`
(`modules/agents/codex/agent.py:343`, `modules/agents/claude_agent.py:568`,
`modules/agents/opencode/agent.py:554`), and only then does `cancel` call
`turn.task.cancel()`. The emit therefore *always* precedes the stop settlement, so the
branch dismissed as the unlucky one was the only branch that ever ran: `canceled` was
reachable in production only when the backend could not emit at all (`stop_failed`, the
stale-release path, an IM `/stop` with no live turn).

Second, `succeeded` is not true. The row's terminal status is not "did the process
stop cleanly" but "did this run produce its result", and the body here is empty
precisely because nobody produced one. A user who ends a run and finds it filed as a
success has been told the opposite of what happened, and a success counter agrees.

The fix is the same principle as §5.8: the emit knows what it is, so it says so.
`stop_output_for` (shared, one place) sends the stop with `completes_turn=True` — the
turn really did end, the dot settles, the SSE waiter closes — and `completes_run=False`,
so an empty body never becomes a terminal status. On its own that would read as
`turn_only_result` (§5.8's Activity case, where another owner genuinely holds the row)
and strand the run `running` until the sweep called it `orphaned`; so the output also
carries an explicit `settled_by=stopped`, and `_turn_release_settlement` now lets a
named settlement win over anything it would infer. Both lanes then reach the writer
that already maps `stopped` to `canceled` with `interrupt_reason=stopped` — no new
status logic, no duplicated i18n. `settle_bound_turn_sink` stays exactly as it was: a
fallback for the stop that gets no emit at all.

Built with `dataclasses.replace`, so a request carrying its own output policy (Activity
lineage, an explicit `run_id`) keeps it and only the lifecycle is overridden.

The defect was copied verbatim into three backends, which is what HFR-040 is for: it
asserts on the terminal emit inside *every* `handle_stop`, so a fourth backend reaching
for `terminal_output_for` in its stop path fails a test instead of silently reporting
stopped runs as successes. (`notify` emits on the refusal paths are excluded — they
settle nothing and return `False`.)

| Mutation | Verdict |
| --- | --- |
| M29 stop output back to `completes_run=True` (the reported bug) | KILLED (HFR-036, HFR-037) |
| M30 drop the explicit-settlement override in `_turn_release_settlement` | KILLED (HFR-036, HFR-037) |
| M31 revert the stop emit to `terminal_output_for` — codex | KILLED (HFR-039, HFR-040) |
| M31 revert the stop emit to `terminal_output_for` — claude | KILLED (HFR-040) |
| M31 revert the stop emit to `terminal_output_for` — opencode | KILLED (HFR-040) |

## 6. Staging

- **PR-A (Gap A)** — §3.1–3.4. Small, no new config, no sweep. Independently
  shippable and fixes the only reproducible-on-demand zombie.
- **PR-B (Gap B)** — §4. Depends on PR-A only for shared constants/i18n keys.
  Must land after, or be explicitly rebased on, PR2 if PR2 lands first (both touch
  `_inflight_sessions` release semantics; PR2's teardown cancel makes B3 rarer but
  not unreachable).

## 7. Design points — resolved at review

- **O1 — canceled vs failed.** Resolved: settle through `defer_run_terminal` +
  `settle_deferred_run` inside `_execute_agent_run` (§3.3.1); confirmed
  `update_run_status` has no status predicate and would clobber `canceled`.
- **O2 — `transport_unavailable` terminal.** Resolved as drafted: `failed` +
  `interrupt_reason`, per the closed status vocabulary. A `vibe runs retry` helper is a
  separate feature, not a blocker.
- **O3 — orphan grace.** Resolved: no race exists (`_spawn_execution` registers the
  owner synchronously with the claim), so 120 s is headroom, not a mitigation.
- **O4 — held-row TTL.** Resolved: before declaring `queue_hold_expired`, the sweep
  makes one `recover_persisted_agent_run_queue(session_id)` attempt (the same call the
  post-completion hook already makes, `core/scheduled_tasks.py:2464-2478`); only if the
  row is still held does it terminalize.

  **Not implemented in PR-B (deviation 8, deliberate).** The recovery attempt would have
  to happen between candidate selection and the terminal write, i.e. inside the store
  method — and the store cannot call the controller without inverting the layering. The
  hold class is safe without it: its TTL is 3600 s and its clock is `updated_at`, so any
  successful recovery (which touches the row) resets it, and the post-completion hook
  already retries recovery on the normal path. Doing it properly means splitting the
  sweep into select → caller-side recovery → re-check → settle; worth it only if a real
  `queue_hold_expired` is ever observed on a session that would have recovered.
  `test_sweep_retries_queue_recovery_before_expiring_a_hold` is therefore not in the
  delivered suite; `test_sweep_expires_a_workbench_queue_hold_only_after_its_ttl` covers
  the TTL itself.
- **O5 — `agent_run`-only scope.** Resolved: keep the scope for now; `_inflight_executions`
  already covers `scheduled`/`task_run` uniformly, so widening B3 is a small delta if
  PR2/PR7 timing slips.

Reviewer's authority question, recorded: PR-A is **not** folded into the approved plan's
PR2. PR2 handles externally cancelled mid-flight executions; Gap A handles executions
that return *normally* having never dispatched. Different triggers, same DB symptom —
and after §3.3.1 both use the same guarded writer, so there is one settlement authority
in the storage layer even with two call sites.

Deliberately deferred (pre-existing, not caused here): a targeted test for
`settle_bound_turn_sink` racing a real terminal emit (`core/session_turns.py:1836`).
