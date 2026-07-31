# PR2 Implementation Plan — P3: Reconcile on Teardown

Status: revision 3, after two rounds of the mandated pre-code review (§9) by
Codex gpt-5.6-sol (session `019fb7cb-3e99-7f91-8d50-84ba733b5abc`; rev 1: NOT
READY, 10 findings; rev 2: findings 2/4/5/6/9/10 closed, 3/7/8 reopened with
required edits, plus one new reconciler blocker). Every finding is closed
below; the finding number is cited where it forced the change.

Authoritative spec: `docs/plans/harness-run-reliability.md` — §4 PR2 (all
2026-07-27 corrections), §5, §6, §7 D1, §10 (esp. 10.8, 10.10). Where this doc
and the master plan disagree, the master plan wins.

## Preconditions verified on master @ 915084b3 (review finding 1: all confirmed)

| Claim | Verified |
|---|---|
| PR6 merged; owed-notice machinery available | `_owed_failure_notice_for_transition` (`storage/background.py:1169`), `_merge_owed_failure_notice` (`:1270`) folded into the same UPDATE by `settle_run_terminal` (`:3525`) and `settle_deferred_run` (`:3706`); drain `_spawn_failure_notice_drain` on the 2 s tick |
| Settlement causes reserved | `core/run_settlement.py:95-97` `INTERRUPT_REASON_EVICTED / RESTARTED / LIFETIME_TIMEOUT` |
| Defect live: cancel requeues | `core/scheduled_tasks.py:4951-4954` `except asyncio.CancelledError: self.request_store.requeue(request.id)` |
| Defect live: stop() requeue comment | `core/scheduled_tasks.py:2774-2777` |
| `cancel_session_executions` absent | grep: 0 matches |
| Join-path state exists | `_session_lock_owners` (`:2458`), `_session_lock_cache` (`:2460`), `_canonical_session_lock` (`:4793`), `_reserve_runtime_session` (`:5939`), `_on_execution_done` (`:4833`) |
| Manager-lane precedent | `SessionTurnManager.release_for_backend_refresh` (`core/session_turns.py:1776`): sets `stop_no_flush`, records `cancel_settled_by` **before** `task.cancel()`, awaits the cancelled tasks |
| Manager settlement gate | `_settle_turn_owned_agent_runs` (`core/session_turns.py:811`) settles only causes in `SETTLEMENTS_WITHOUT_RESULT` — new causes must be added there or manager settlement silently no-ops |
| Non-terminal status tuple duplicated | `core/services/session_fork.py:36`, `storage/workbench_sessions_service.py:52` |
| Eviction entry | `core/handlers/session_handler.py:1600` `evict_idle_sessions`; orphan reaper entry `reap_orphaned_claude_sessions` (`:1696`) |
| End path | `core/services/running_agents.py:1133` `end_running_agent` → `_stop_active_agent` (`:961`) |

Line numbers are orientation only; resolve by symbol at edit time.

## Scope

In: cause-aware cancellation of in-flight executions on every reachable
run-blind teardown path; terminalize-not-requeue on cancellation (including the
legacy file-store fallback and coalesced siblings); durable cause metadata
through the defer/settle pair; run↔session association for `create_per_run`;
generic cause-aware manager-lane cancellation; a **session-scoped** teardown
reconciler; owed-notice stamping (PR6's drain delivers); shared non-terminal
constant; i18n; Running-tab End made settle-first/teardown-second.

Out (review finding 6): **no change to `sweep_stale_runs`' orphan predicate, no
change to `recover_processing_runs`, no crashed-process or global restart
recovery** — those are PR7's assigned changes (§5). PR2's recovery claim is
limited to teardown events this process observes. Also out: PR3 pinning, PR4
drain supervision, D7. No new run status values (§3.1).

## Settlement contract (review finding 3 — closed here, once)

All causes flow through `core/run_settlement.py`; **no call site decides status
or copy**:

- Add `SETTLED_BY_EVICTED` and `SETTLED_BY_RESTARTED` (names aligned with the
  file's conventions) to:
  - `SETTLEMENTS_WITHOUT_RESULT` (`:72`) — else `_settle_turn_owned_agent_runs`
    silently no-ops for them;
  - `SETTLEMENT_TERMINAL_STATUS` (`:212`) → **`failed`** for both;
  - `SETTLEMENT_I18N_KEYS` (`:191`) → new i18n keys (en + zh).
- `SETTLED_BY_STOPPED` stays → `canceled`, no interruption notice (HFR-012/037).
- **Generic external-cancel default** (a cancellation PR2 did not originate):
  exact constant `SETTLED_BY_INTERRUPTED` with wire value
  `interrupt_reason="interrupted"`, classified as infrastructure → `failed`,
  its own i18n copy, owed notice stamped. Never defaults to `evicted`.
- **Full membership for `SETTLED_BY_INTERRUPTED` (review round 2, finding 3):**
  it joins all five sets, not just the first three —
  `SETTLEMENTS_WITHOUT_RESULT`, `SETTLEMENT_TERMINAL_STATUS → failed`,
  `SETTLEMENT_I18N_KEYS`, **`RUN_INTERRUPTION_REASONS`**
  (`core/run_settlement.py:170` — membership there is what routes it down the
  unsuppressed interruption lane, gives it the `interrupt:{run}:{reason}`
  notice identity, and keeps it out of definition health), and
  **`NOTICE_REASON_I18N_KEYS`** (`core/failure_notices.py:233`) with en/zh
  copy. `evicted`/`restarted` are already in the last two (PR6); PR2 adds them
  only to the first three. The existing parity test
  `tests/test_i18n_backend_keys.py::test_notice_reason_i18n_map_covers_exactly_the_interruption_lane`
  pins the lane and is extended, and is in the owed test list (HFR-124).
- `interrupt_reason` written to run metadata is the settlement's reason string
  (`evicted` / `restarted` / `interrupted`); `stopped` writes **no**
  `interrupt_reason` and **no** owed failure notice.

Rule, stated once, both directions: **the cause is recorded at the cancel site
and never inferred from the path; the status and copy are decided only by the
settlement tables.**

## Work breakdown (ordered)

### Step 0 — reproducing tests first (§10.8)

- `test_cancelling_an_inflight_execution_terminalizes_and_frees_the_session_lock`
  (HFR-100; today it requeues and the lock leaks)
- `test_service_stop_terminalizes_a_scheduled_run_instead_of_requeueing`
  (HFR-101, duplicate-prompt regression)

Home: `tests/test_scheduled_tasks.py`, hermetic per §6 (`ensure_sqlite_state`,
never `metadata.create_all` alone; no `uses_real_paths`).

**Existing-test rewrite (review finding 9):** the HFR-003 cancellation test
currently asserts requeue on the legacy file backend — rewrite it to assert
terminalization; keep its scenario ID (HFR-003), updating the catalog entry
text.

### Step 1 — cause-aware cancel primitive

- Map `self._execution_cancel_causes: Dict[str, str]` (request/run id →
  settlement cause) in `ScheduledTaskService`.
- `_cancel_execution(run_id, settled_by)` — records the cause, then
  `task.cancel()`. Every cancel site calls it; raw `.cancel()` on execution
  tasks becomes lint-greppable dead style.
- Cleared in `_on_execution_done` and `stop()` alongside the existing maps.
- Handler default when no cause recorded: `SETTLED_BY_INTERRUPTED` (contract
  above) — never `evicted` (HFR-109).

### Step 2 — terminalize instead of requeue

`_execute_claimed_request`'s `except asyncio.CancelledError` branch:

- stop calling `request_store.requeue(request.id)`;
- terminalize **every claimed request, no `request_type` inspection**;
- settle the primary run **and every id in `coalesced_completion_ids`**
  (review finding 9; HFR-111) — a cancellation that settles only `request.id`
  strands the coalesced siblings;
- SQLite path: `defer_run_terminal` → `settle_deferred_run` (guarded), with
  status from `SETTLEMENT_TERMINAL_STATUS[cause]`, `error` naming the
  interruption, `interrupt_reason` per the contract. Never `update_run_status`.
- **Durable cause through the defer/settle pair (review finding 2, blocker):**
  `defer_run_terminal` gains a `metadata: Optional[dict]` argument persisted
  into `result_payload_json` (alongside the existing deferred fields);
  `settle_deferred_run` pops it and passes it as `extra_metadata` to
  `_merge_owed_failure_notice` **inside the same guarded terminal UPDATE**, so
  `interrupt_reason` survives a crash between defer and settle and the stamper
  sees it (HFR-110). Extend both SQLite methods and their `TaskExecutionStore`
  wrappers (`core/scheduled_tasks.py:1884` region).
- **Legacy file-store fallback (review finding 9):** `defer_run_terminal` /
  `settle_deferred_run` return `False` on the file backend; on that backend
  terminalize via `complete(..., ok=False)` carrying the interrupt reason
  (HFR-112). No backend is left on the old requeue behaviour.
- `stop()` cancels with `SETTLED_BY_RESTARTED`; delete the stale "requeues the
  run" comment. Do not touch `requeue_on_return` (`:4939`) — workbench-queue
  hold, not a cancellation.
- §10.10 guardrail: no new caller of the live failure emitter — the stamp +
  PR6's drain is the delivery path.

### Step 3 — `cancel_session_executions(session_id, *, settled_by, include_manager_lane=True) -> SessionCancellationResult`

Result type is exact: `SessionCancellationResult(cancelled_count: int,
claimed_run_ids: frozenset[str])` (review round 4 — the earlier `-> int`
contradicted the snapshot the body returns).

Signature carries the cause (review finding 3) plus
`include_manager_lane: bool = True` (review round 3: Running-tab End must
cancel the scheduler lane only, leaving the manager turn to the canonical
user-Stop path — without the flag, End's step 1 would have already cancelled
and awaited the manager turn and step 2 would fall through to a degraded stop
path). **Before cancelling anything, the helper snapshots ownership** —
awaiting the cancellation triggers `_on_execution_done` and
`SessionTurnManager._run` cleanup, which erase exactly the ownership evidence
the reconciler needs (review round 3). The snapshot is deliberately
**process-scoped, not session-filtered** (review round 4): **all** run ids
currently owned by `_inflight_executions`, unioned with the manager-owned run
ids — a `create_per_run` execution whose dedicated session map was missed (the
HFR-113 case) has no session-keyed entry, and a session-filtered snapshot
would exclude exactly that row; the session scoping happens later, at the
reconciler's intersection with `agent_runs.session_id`. The helper **awaits
the cancelled tasks**, then returns `SessionCancellationResult` so callers
pass `claimed_run_ids` to the step-5 reconciler. Three paths, no shared code,
each tested:

1. **Scheduler lane, ordinary pinned session:** session id →
   `_session_lock_cache` / `_canonical_session_lock` → lock key →
   `_session_lock_owners` → run id → `_inflight_executions` →
   `_cancel_execution(run_id, settled_by)` (HFR-103).
2. **Scheduler lane, `create_per_run` (review finding 5):**
   `_reserve_runtime_session` gains an optional `execution_id` parameter,
   passed only from the `create_per_run` execution call sites (the callee has
   no run id today). On reservation success it records, **ordered, not
   transactionally atomic** (map first, then row): `execution_id → session_id`
   in a new map, then stamps the reserved `session_id` onto the run row
   (guarded update writing only `session_id` + `updated_at` on a non-terminal
   row), so a map-missed row is still findable by the DB reconciler (HFR-104,
   HFR-122). Torn down in `_on_execution_done`, cleared in `stop()`. Not
   `_session_lock_cache` — wrong key direction, corrupts lock keys.
3. **Manager lane (review finding 4):** new generic
   `SessionTurnManager.release_for_teardown(session_id, *, settled_by) -> bool`
   copying `release_for_backend_refresh`'s shape exactly: set
   `stop_no_flush`, record `turn.cancel_settled_by = settled_by` **before**
   `task.cancel()`, await the cancelled task, update manager state and agent
   status. **Not** `cancel(session_id)` (user-Stop API, settles `canceled` —
   HFR-029 inversion), and not a pretend backend refresh. Running-tab End
   keeps using the existing user-facing stop path with `stopped`.

### Step 4 — wiring matrix (review finding 7 — explicit, per entry symbol)

Uniform ordering at every entry:
**record cause → cancel both lanes (awaited) → session-scoped DB reconcile →
backend teardown.** Settle first, tear down second — a torn-down backend can no
longer settle its own turn.

| Entry symbol | Cause (`settled_by`) | Notes |
|---|---|---|
| `evict_idle_sessions` — ordinary idle branch | `evicted` | two-hop resolve from `composite_key` via `agent_sessions` (§3.3); extend the existing eviction suite in `tests/test_claude_cli_path.py` (HFR-120) |
| `evict_idle_sessions` — stuck-active backstop branch | `evicted` | same helper, both branches (HFR-106 covers the manager lane) |
| `cleanup_session` | **parameter, required** — each caller passes its cause; no inference inside the helper | full caller inventory below (review round 2, finding 7) |
| `reap_orphaned_claude_sessions` | **not session-keyed — PR7 backstop** | it discovers untracked OS processes with no session id and returns only a count; it cannot call a session-keyed API without new identity recovery. Classified with the non-interceptable rows below; do **not** label ownerless process death `evicted` (rev 2 wrongly claimed it "can call the helper") |
| Controller shutdown — Claude/Codex/OpenCode (`controller.py` shutdown path) | `restarted` | all three backends in PR2 (§8 decision) |
| Codex `evict_idle_transports` | `evicted` | |
| `ScheduledTaskService.stop()` | `restarted` | step 2 |
| Running-tab End (`end_running_agent`) | `stopped` via the canonical stop path | carve-out below |
| `process_isolation.py` kill paths, kernel OOM (`resource_governance.py`), hard crash, orphan reaper | **not interceptable in-process / not session-keyed** | backstop is restart-path recovery = **PR7** (§5 already assigns it); named here so the deferral has a number, per the master plan's rule |

**`cleanup_session` caller inventory (audited 2026-07-31, review round 2
finding 7 — assigned now, not "at edit time"):**

| Caller (symbol / region in `core/handlers/session_handler.py`) | Cause |
|---|---|
| `_reuse_cached_claude_session_if_available` — Model Hub channel changed (`:206`), system prompt changed (`:225`), caller env changed (`:237`), Git PATH changed (`:248`) | `SETTLED_BY_BACKEND_REFRESH` (existing constant — these are runtime recreations, not evictions; `:206` already waits for idle first) |
| Subagent-client analogues — three checks (`:288`, `:305`, `:316`) | `SETTLED_BY_BACKEND_REFRESH` |
| Stuck-active eviction fallback (`:1689`, when `force_cleanup_stuck_active_session` is unavailable, and `:1691`) | `evicted` |
| Broken-transport receivers — concurrent read error (`:1769`), SDK buffer fatal (`:1785`) | `SETTLED_BY_INTERRUPTED` (infrastructure fault mid-turn; there is no idleness or restart claim to make) |
| `core/services/running_agents.py:627` — End, non-active branch | part of the End orchestration below (`stopped`) |

A new `cleanup_session` caller added later fails loudly: the cause parameter is
required, so the compiler/reviewer sees the decision rather than inheriting a
default.

**Running-tab End orchestration (review finding 8 — exact, two lanes):**

In `end_running_agent`, in order:

1. **Scheduler lane only:** `cancel_session_executions(session_id,
   settled_by=SETTLED_BY_STOPPED, include_manager_lane=False)` —
   unconditional, whatever the state read returned; closes the idle-state race
   where a scheduler-lane execution acquires the session after the read (rev 2
   left this lane uncovered because `_stop_active_agent` only reaches the
   manager). The manager turn is deliberately left untouched here.
   Round-7 carve-out (HFR-328): unconditional in STATE, but scoped by provable
   backend ownership of the lane — when ownership cannot be proven the cancel is
   refused, because a wrongly killed foreign turn is unrecoverable while an
   unsettled run is swept.
2. **Manager lane:** the existing user-facing stop path unchanged
   (`_stop_active_agent` → `SessionTurnManager.cancel` /
   `command_handler.handle_stop`) — it already records `SETTLED_BY_STOPPED`
   and preserves backend Stop behaviour. The idle case is silent because the
   **canonical stop context** sets `suppress_stop_no_active_notice`
   (`_build_session_row_stop_context`, `core/services/running_agents.py:933`)
   — provided by that context, not by any new helper (HFR-107, HFR-108).
3. **Await both** settlements.
4. Session-scoped reconcile with the narrowed predicate (step 5), cause
   `stopped`.
5. Backend teardown, **idempotent/convergent**: "already absent" after a
   successful stop counts as torn down (the Workbench-stop path may already
   have removed the Claude client —
   `test_end_active_workbench_turn_settles_via_manager` pins that a successful
   End must not degrade to `session_not_live` through duplicate cleanup).
   Precedence: the stop's settlement outcome is authoritative; teardown
   failures are logged and surfaced as the endpoint's error only when the stop
   itself also failed. "Teardown even when stop failed" applies to **all three
   backends including OpenCode** — the rev 2 hedge is removed; if
   implementation uncovers a hard OpenCode constraint, the OpenCode leg gets a
   numbered dependent PR and a dependency edge before PR2 merges, never a
   silent preservation of the old semantics.

### Step 5 — session-scoped teardown reconciler (review finding 6 — redefined)

A new, **session-scoped** reconciler on the settlement service, invoked by the
teardown helper **after** the awaited cancellation, over the torn-down
session's rows found via `agent_runs.session_id` (which step 3.2 now
guarantees is populated for `create_per_run`). Settle via guarded writers with
the entry's cause and stamp the owed notice (HFR-113). In the happy path it
finds nothing — the cancel got there first.

**Narrow predicate (review round 2, new blocker; ownership evidence per round
3):** the reconciler settles only **demonstrably interrupted claimed/running
work**. The selection is a three-way intersection (review round 4):

`pre-cancel process-owned ids (claimed_run_ids)` ∩ `rows with this
session_id in agent_runs` ∩ `narrowed running/processing predicate below`

The in-memory maps are already erased by `_on_execution_done` /
`SessionTurnManager._run` by the time the reconciler runs, so the snapshot is
the only surviving proof of which claims this process held — and because the
snapshot is process-scoped, the map-missed `create_per_run` row (found via its
early `agent_runs.session_id` stamp) stays **inside** the intersection
(HFR-113 explicitly proves this: a DB-associated run absent from the dedicated
session map remains in the snapshot and is reconciled). An unrelated `running`
row on the same session that this process never claimed is never settled
(HFR-125). It must **exclude**:

- `queued` rows that never started — a queued run pinned to the session is
  future work, not interrupted work; it survives teardown and is dispatched
  later (or handled by PR3's pin / PR7's recovery) (HFR-114);
- gate-parked followers carrying `workbench_queue_holds_run` — D1's §7
  carve-out: `running` there means "accepted, not yet started" (HFR-123);
- `watch_runtime` rows and deferred-terminal rows — the same exemptions
  `recover_processing_runs` and `sweep_stale_runs` already honour.

`settle_run_terminal` is guarded to `queued|running`, which is **wider** than
this predicate — the narrowing lives in the reconciler's selection, not in the
writer.

Explicitly **not** in PR2: widening `sweep_stale_runs`' `:2425`-region orphan
predicate, touching `owned_run_ids` semantics, or `recover_processing_runs` —
PR7's assigned work. PR2's step-1 cancel is what makes evicted rows leave the
owned set so the *existing* sweep regains visibility; that interaction is
tested, not modified.

### Step 6 — shared constant + i18n

- Promote one shared non-terminal-status constant; replace the copies in
  `core/services/session_fork.py:36` and
  `storage/workbench_sessions_service.py:52`; no fourth copy.
- All interrupted-run copy through `vibe/i18n/` (en + zh), keys registered in
  `SETTLEMENT_I18N_KEYS`. No key-parity test exists — verify both files by
  hand.

### Step 7 — scenario catalog + docs

Exact IDs assigned up front (review finding 10; §10.7). Block HFR-100…129
(overflow HFR-320…349); unused IDs stay reserved to PR2:

| ID | Test |
|---|---|
| HFR-100 | `test_cancelling_an_inflight_execution_terminalizes_and_frees_the_session_lock` |
| HFR-101 | `test_service_stop_terminalizes_a_scheduled_run_instead_of_requeueing` |
| HFR-102 | `test_evicted_scheduled_run_without_a_callback_target_still_notifies` (delivered notice via PR6 drain — exit criterion) |
| HFR-103 | `test_cancel_session_executions_finds_a_pinned_session_execution_via_lock_owners` |
| HFR-104 | `test_cancel_session_executions_finds_a_create_per_run_execution` |
| HFR-105 | `test_cancellation_cause_distinguishes_eviction_from_shutdown` |
| HFR-106 | `test_evicting_a_session_cancels_its_workbench_turn_and_fails_the_run_as_evicted` (asserts status **and** reason) |
| HFR-107 | `test_ending_an_active_row_settles_the_run_canceled_with_no_interruption_notice` |
| HFR-108 | `test_ending_an_idle_row_is_silent_and_settles_nothing` |
| HFR-109 | `test_external_cancellation_without_recorded_cause_settles_interrupted_not_evicted` |
| HFR-110 | `test_interrupt_reason_survives_a_crash_between_defer_and_settle` |
| HFR-111 | `test_cancellation_settles_coalesced_sibling_runs` |
| HFR-112 | `test_file_store_cancellation_terminalizes_via_complete` |
| HFR-113 | `test_session_scoped_reconciler_settles_a_map_missed_row` |
| HFR-114 | `test_queued_not_started_run_survives_teardown_reconciliation` |
| HFR-115 | inbox-events: running row → `failed` + `completed_at` set |
| HFR-116 | inbox-events: callback stays `pending` and is discoverable by the drain |
| HFR-117 | inbox-events: reconcile idempotency (second pass writes nothing) |
| HFR-118 | inbox-events: stronger-terminal race (arbitration preserved) |
| HFR-119 | inbox-events: callback `sent`/`skipped` preserved across reconcile |
| HFR-120 | eviction suite extension: both `evict_idle_sessions` branches invoke the teardown helper |
| HFR-121 | `release_for_teardown` records `cancel_settled_by` before cancel and awaits settlement |
| HFR-122 | reservation stamps `session_id` onto the `create_per_run` run row |
| HFR-123 | `test_gate_parked_follower_survives_teardown_reconciliation` |
| HFR-124 | extension of `test_notice_reason_i18n_map_covers_exactly_the_interruption_lane` for `interrupted` |
| HFR-125 | `test_reconciler_does_not_settle_an_unrelated_running_row_on_the_same_session` |
| HFR-003 | (rewrite of existing) cancellation terminalizes instead of requeueing on the legacy backend |

HFR-115…119 live in `tests/test_inbox_events.py` per §6 (rev 1 omitted them).
Rev 1's claim that `evict_idle_sessions` has no behavioural tests was **false**
— the suite exists in `tests/test_claude_cli_path.py:1522`; PR2 extends it.

Also: catalog entries under `tests/scenarios/`, scenario IDs visible in tests
and PR description; update the master plan's status table (PR6 → merged
2026-07-31, PR2 → in review) and the P3 landing-evidence row.

## Guardrails carried from §10

- §10.3: equality rule — never write text contradicting the status left in
  place.
- §10.10: no new live-emitter caller for replay; stamp + PR6 drain only.
- §10.8: every "X is safe because Y does Z" above is a hypothesis; the test
  comes first.

## Process

- Branch `fix/harness-teardown-reconcile` (this worktree), from `master`
  @ `915084b3`. Commits `fix(harness): …` per step where practical.
- Pre-push: `ruff check` on changed Python; no UI build expected.
- PR: non-draft; Codex bot review gate; `background-watch-hook` review-fix
  loop immediately after opening; PR body names capability, scenario IDs, and
  evidence layers (unit / contract / scenario / residual manual).
- No merge on our own initiative.
