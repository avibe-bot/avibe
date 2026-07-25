# Agent Run Zombie Settlement: honest terminal state + staleness sweep

Status: reviewed by `codex-expert` (APPROVE WITH CHANGES, 2026-07-25); all six blocking
items folded in below — ready to implement

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

Notes:

- B1's precondition should be **recorded, not re-derived**: have
  `_drain_requests` stamp `metadata.last_skip_reason` (+ a coarse `last_skip_at`) on the
  skip branches (`:2023`, `:2026`, capacity `:2017`), so the sweep never guesses and
  the row itself explains why it sat. A row skipped only for capacity or a session lock
  is **never** swept — it is making progress.

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

### 4.4 Config

New keys under runtime config in `config/v2_config.py`, each with the defaults above
and `0` meaning "disabled":
`harness_run_orphan_grace_seconds`, `harness_run_queued_ttl_seconds`,
`harness_run_hold_ttl_seconds`, `harness_run_sweep_interval_seconds`.
No UI surface in this plan (defaults are the product decision); document in
`docs/`. All user-visible error copy goes through `vibe/i18n/` — note
`core/scheduled_tasks.py` imports no i18n today, which the approved plan already
flags as a violation at this exact seam.

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
