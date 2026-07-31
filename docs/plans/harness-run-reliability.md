# Harness Run Reliability: Settlement, Reconcile, Delivery, and Visibility

Status (2026-07-29): **PR1 and PR5 merged. PR6 in review. PR2/PR3/PR4 not
started. PR7 BLOCKED on D7.** Defects P3, P4a, P4b still reproduce on `master`.

| PR | Problem | Status |
|---|---|---|
| PR1 | P2 delivered runs record no `result_text` | **#1063 — merged 2026-07-28** |
| PR5 | P5 pinned bindings break | **#1064 — merged 2026-07-29** |
| PR6 | P6 failures invisible | **#1072 — in review**, CI green |
| PR2 | P3 teardown never reconciles | not started (needs PR6) |
| PR3 | P4a eviction blind to queued work | not started (needs PR2) |
| PR4 | P4b drain loop unbounded | not started (needs PR3) |
| PR7 | P1 settle at the real terminal result | **BLOCKED on D7** |

> **Do not begin PR7.** §7's D7 is open: whether a crash-recovered `claimed`
> message row is resumed or failed. Resuming can duplicate agent side effects
> (posts, tool calls, spend); failing can discard work the backend never
> received; the durable record cannot distinguish the two. An implementer who
> follows a top-level "approved" into that branch will pick one and be wrong
> half the time. Everything else may proceed.

## How to read this document

**Resolve every code reference by symbol, not by line.** Line numbers are
relative to `5921ad39` (2026-07-25) and have drifted; `core/scheduled_tasks.py`
alone took 17 commits in the following 30 days.

**"Approved" means no load-bearing choice is still open inside that PR** — not
merely that its findings are agreed. The failure mode is a choice phrased as an
implementation note ("include or exclude it deliberately", "confirm X… if so,
add a fallback") that an implementer resolves by picking the cheaper branch.
There is no grep for this: hedges are semantic, not lexical. The only reliable
check is reading each PR's section end to end asking *"could an implementer here
choose the cheaper thing and still claim compliance?"*

**A correction that names where else it must be applied is not finished until it
is applied there.** Every cross-section defect found in review was created by a
correct local fix that was never propagated.

**§10 records what implementing PR1, PR5, and PR6 proved about this plan.** Read
it before starting any remaining PR: it corrects claims that survived 43 rounds
of document review and were only falsified by running the code.

Origin branch: `fix/harness-run-reconcile` (from `master` @ `5921ad39`).

### Relation to `agent-run-zombie-settlement.md` (#1005, landed 2026-07-26)

That plan is a **delta** cut from this one: two zombie classes specific to
`run_type='agent_run'`. It shipped the shared substrate these PRs build on —
`core/run_settlement.py` (settlement vocabulary), the guarded metadata-merging
writers, `sweep_stale_runs` (three staleness classes), and
`_release_leaked_session_locks`. Its own non-goals name this plan: *"turn-duration
timeout; PR7's scheduled/watch settlement change; PR2's teardown cancel; PR6's
notification ladder."*

Two consequences worth stating up front:

1. `core/run_settlement.py:14` already reserves `evicted` / `restarted` /
   `lifetime_timeout` for this plan **by filename**. Four references to this
   document exist in merged upstream code and docs.
2. **The landed sweep structurally cannot reach the eviction path.** It exempts
   rows in `owned_run_ids`, and ownership is membership in `_inflight_executions`.
   Eviction kills the backend without cancelling the execution task, so that task
   stays in the set forever and the row is reported "owned" forever — the
   `orphaned` class never fires on it. The same predicate defeats
   `_release_leaked_session_locks`, whose `leaked` test is
   `run_id not in self._inflight_executions`. #1005 says so explicitly (§1.1 B3:
   *"PR2 covers the eviction path (cancel + reconcile at teardown)"*). **PR2 below
   is the only fix; the `vibe restart` wedge of P3 is untouched by two safety nets
   that both look like they should catch it.**

### Landing evidence for the problems still open

| Problem | PR | Load-bearing evidence (verified against `35a5e13a`) |
|---|---|---|
| P3 teardown never reconciles | PR2 | `grep -E "agent_runs\|request_store\|run_id" core/handlers/session_handler.py` → 0 matches. `cancel_session_executions` does not exist. |
| P4a eviction blind to queued work | PR3 | `evict_idle_sessions` still reads only the clock and in-memory maps, in both passes. |
| P4b drain loop unbounded | PR4 | `_drain_recovered_activity_outputs` is still the first inline `await` of every `_watch_store` tick; zero `wait_for` / `heartbeat` / `watchdog` in the file. |
| P6 failures invisible | PR6 | `_task_last_status` byte-identical; no `emit_backend_failure` caller in `core/scheduled_tasks.py`. **Fix in review: #1072.** |
| P1 settle at dispatch | PR7 | `TaskExecutionResult` still has no `complete_on_return`; only its sibling `AgentRunExecutionResult` does. |

One partial credit: `9b1af0a5` added a row-level alert channel to the Harness list,
but it is driven by `lifecycle_detail`, which returns `None` unless the row is
`finished` — and a cron definition is never `finished`. A recurring task failing
daily still renders identically to one succeeding daily. See P6.

## 1. Background

Operating the local Avibe Harness (three active cron tasks plus ad-hoc
`vibe agent run`) surfaced a cluster of defects that share one theme: **the
`agent_runs` record is not a faithful model of what the Agent actually did.**
A run can be marked terminal before the turn starts, can stay non-terminal
forever after its session is destroyed, can sit unclaimed for an hour, and can
fail every day without anyone being told.

Prior art in this repo:

- `docs/plans/agent-run-harness.md` — the Harness model (definitions, runs, callbacks)
- `docs/plans/agent-run-terminal-lifecycle.md` — fixed premature settlement, but
  **explicitly scoped to `request_type == "agent_run"`**: *"Stored tasks and
  watches may still mean 'trigger/follow-up submitted'; changing that semantic
  together would be a broader product migration."* P1 below is that deferred
  migration.
- `docs/plans/agent-run-callback-session.md` — callback routing; assumes
  terminality is real.
- `docs/plans/agent-run-scope-semantics.md`, `agent-run-target-resolver.md` — target resolution.

### 1.1 Corrections to earlier diagnoses

Four widely-repeated assumptions from the earlier hand diagnosis are **wrong**
and must not survive into implementation:

| Assumption | Reality |
|---|---|
| P2's fork is `post_to` | It is `suppress_delivery`, derived from session visibility (`visibility == "background"`, `core/scheduled_tasks.py:319`; pre-migration: `session_metadata["no_delivery"]`). The `post_to` correlation is incidental. |
| `agent_runs.pid` can drive reconcile | `pid` is **never populated** — 0 of 233 live rows. `update_run_status(..., pid=...)` exists but no caller passes it. Unusable. |
| Nothing sweeps non-terminal runs on restart | `recover_processing_runs` (`storage/background.py:1563-1587`) resets `running|processing` → `queued`. It **requeues, it does not terminalize** — and that is a duplicate-prompt hazard — see D1. |
| The zombie runs were cleared by hand | The restart sweep cleared them. `7b459e5caea7` settled `succeeded` via the deferred-Activity path; `96e10711797b` settled `canceled`. Both had `cancel_requested=1` **16 minutes** before terminalizing — that gap is the real evidence. |
| P4 was caused by idle eviction | Eviction was a symptom. The delivery failure was a **~65-minute stall of the `_watch_store` drain loop**; once it resumed, the message was delivered to a transparently re-spawned session. |

## 2. Problem inventory (with live evidence)

### P1 — Scheduled/watch runs settle at dispatch, not at turn completion

`run_type='scheduled'` rows reach `status='succeeded'` + `completed_at` ~0.6s
after `created_at`, while the real turn takes 60–120s.

| run | definition | status | dur(s) | result_text | updated_at − completed_at |
|---|---|---|---|---|---|
| `80437cb2fcf0` | `b9596cbd1855` support-daily | succeeded | 0.62 | 3471 chars | **+80s** |
| `c222d4c5d55e` | `3848228cb675` crypto-board | succeeded | 0.62 | **0 chars** | 0s |

**Root cause chain:**

1. `core/scheduled_tasks.py:2362` — `should_complete = True` by default; the
   `{"task_run","scheduled"}` branch (`:2368`) never clears it. Only the
   `agent_run` branch (`:2408-2431`) can set `complete_on_return=False`;
   `TaskExecutionResult` (`:211-215`) has no such field.
2. `core/scheduled_tasks.py:2445-2462` — the `finally` calls
   `request_store.complete(...)` → `:1513-1524` writes
   `status="succeeded" if ok else "failed"` + `completed_at` unconditionally.
3. `_execute_request` returns at prompt submission (`:2757`
   `handle_scheduled_message` → `core/handlers/message_handler.py:64-82`), not at
   turn completion. For avibe targets it returns even earlier (`:2755-2756`),
   with an in-code comment stating this is intentional.
4. The `agent_run` path settles correctly **only** because it uses a different
   entry: `:2598-2609` passes a non-`None` `on_chunk` to `dispatch_turn`, which
   flips `core/services/dispatch.py:81-83` → `:114-120` (`await done.wait()`).
   The scheduled path registers no sink.
5. Once dispatch stamps terminal, the deferral machinery is **structurally dead**
   for that row: `defer_run_terminal` returns `False` on terminal rows
   (`storage/background.py:1409-1413`) and `settle_deferred_run`'s UPDATE is
   scoped to `queued|running` (`:1503-1510`).
6. `run_definitions` health is stamped at dispatch too:
   `core/scheduled_tasks.py:2516 mark_task_result` runs immediately after
   `_execute_request` returns.

### P2 — Delivered runs never record `result_text` — FIXED (#1063, merged 2026-07-28)

The fork was `suppress_delivery`, **not** `post_to`: the suppressed path had no
trigger-kind gate and back-filled `result_text`, while the delivered path
returned at the `task_trigger_kind != "agent_run"` gate and recorded nothing —
the same gate in `core/message_output.py` and `_activity_run_ids`, five sites
total, two of which are one rule (the `elif` below the suppressed-branch gate is
its negation). All 67 live `run_type='watch'` rows had empty `result_text`.
Fixed by widening the gates plus a guarded, status-preserving text backfill;
§10.1–10.3 record what the implementation corrected.

### P3 — Session teardown never reconciles in-flight runs

Verified constants: `config/v2_config.py:30` `DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
= 600`; `:73-74` multiplier 3 / floor 1800 → stuck-active threshold
`max(600×3,1800) = 1800s` (`core/handlers/session_handler.py:1432-1436`). Sweep
cadence is **100s**, not 600s: `core/controller.py:1457
sweep_interval = max(min(enabled_timeouts)//6, 60)`.

`grep -n "agent_runs\|request_store\|run_id" core/handlers/session_handler.py`
returns **zero matches** — the entire teardown surface has no concept of a run.

**The deeper cause:** the authoritative terminal writer is the `finally` of
`_execute_claimed_request` (`core/scheduled_tasks.py:2444-2463`). Eviction kills
the backend *out from under a still-awaiting execution task* without cancelling
it, so:

1. The `finally` never runs → run pinned `running`, no callback.
2. `_on_execution_done` (`:2345-2351`) never runs → **`_inflight_sessions.discard()`
   never happens** → `_drain_requests` (`:2019-2023`) skips every future request
   for that session forever.

**That leaked lock — not the stale DB row — is the actual `vibe restart` trigger.**
A fix that only rewrites the DB row leaves the session wedged.

**Teardown path matrix** (every live-process path is run-blind):

| Path | file:line | Reconciles runs? | Callback? | Kills children? |
|---|---|---|---|---|
| Idle eviction | `session_handler.py:1404`→`:1495` | No | No | Yes |
| Stuck-active backstop | `session_handler.py:1486-1493` | No | Partial (`agent_sessions.agent_status` only) | Yes |
| `cleanup_session` | `session_handler.py:1318-1344` | No | No | Yes |
| Orphan reaper | `claude_process_reaper.py:441-570` | No | No | Yes |
| `process_isolation.py` | `:22-116` `os.killpg` | No | No | Yes |
| `resource_governance.py` | `:398-419` | No (kernel OOM) | No | Yes |
| Controller shutdown (Claude/Codex/OpenCode) | `controller.py:1541-1601` | No | No | Yes |
| Running-tab "End" — active row | `running_agents.py:1182`→`_stop_active_agent:955-1003` | Yes (canonical stop) | **Yes — `canceled`, by design** | No |
| Running-tab "End" — idle row | `running_agents.py:1224-1228`→`:595-727` | No | No | Yes (no turn *at the state read* — §PR2 makes the settle unconditional) |
| Codex `evict_idle_transports` | `codex/agent.py:491-583` | No | Partial | Yes |
| **Restart sweep** | `storage/background.py:1563-1587` | **Yes — requeues, not terminalizes** | No | n/a |

**Enabling fact that makes the fix cheap:** callback dispatch is DB-polled, not
push. `list_pending_callbacks` (`storage/background.py:964-979`) needs only
`completed_at IS NOT NULL` + terminal status + `callback_status='pending'`, and
`_watch_store` ticks every 2s on a plain `await asyncio.sleep(2)`
(`core/scheduled_tasks.py:2035`), consulting `PRAGMA data_version` *within* each
tick to decide whether to reload — the sleep sets the cadence, the pragma gates the
work. **A reconcile that sets
`status` + `completed_at` gets the callback for free — but only for a run that
already has a callback target.** Verified live on both zombies, and that is
exactly the limit of the claim: both were `agent_run` rows carrying a
`callback_session_id`.

`list_pending_callbacks` also filters `callback_session_id IS NOT NULL AND != ''`,
and `enqueue_definition_run` (`core/scheduled_tasks.py:923-975`) never sets that
column. So for an ordinary `scheduled` run — or an agent run created with an
explicit no-callback policy — terminalizing the row notifies **nobody**. Those
cases need PR6's notification path; see PR2 correction 2. An earlier revision of
this paragraph stated the free-callback result unqualified, which would have led
an implementer straight into the silent-failure D1 forbids.

### P4 — Delivery into an existing session has no liveness guarantee

Three verified facts:

**(a) Enqueue cannot refresh `last_activity`.** `session_last_activity` is a
process-local in-memory dict (`core/controller.py:198`). `vibe agent run` is a
**separate OS process** that only writes a SQLite row (`vibe/cli.py:4245-4265`).
The first touch happens deep inside the turn, at the
`get_or_create_claude_session` call in `claude_agent.py:110` — the function itself
lives in `core/handlers/session_handler.py:746`, which is where the behaviour to
change is.

**(b) `evict_idle_sessions` is blind to queued work** (`session_handler.py:1441-1454`):
it consults only the clock and in-memory maps, never `agent_runs`.

**(c) The field incident was a 65-minute drain stall, not eviction.**

| run | created (UTC) | started | outcome |
|---|---|---|---|
| `96e10711797b` | 23:14:15 | **NULL** | canceled at 00:19:58 (restart) |
| `24be5f00440f` | 00:05:52 | 00:19:58 | succeeded |

Log: eviction at 23:21:36 UTC reported `652.4s idle`, i.e. `last_activity`
frozen at the session's *birth* turn (23:10:43) — direct proof of (a). Nothing
moved until `vibe restart` at 00:19:47, after which the run was delivered to a
transparently re-spawned session within 11s. **The run was never lost; the drain
was asleep.**

Suspected stall site (high confidence, not proven): `_watch_store`
(`core/scheduled_tasks.py:1884-1907`) awaits `_drain_recovered_activity_outputs()`
**inline, first, every tick**, and that path awaits `emit_agent_message` with no
timeout (`:1753`). The same window shows a failure storm on exactly that
machinery (`message_mirror.py:480 persist_agent_message: failure`,
`claude_agent.py:1036 Activity output was not durably persisted`), and two stuck
activities were flushed at restart.

**Delivery semantics today:** pickup is at-least-once and durable (the
`agent_runs` row is the queue; `claim_pending_run` is a conditional UPDATE), but
the **backend send is at-most-once and silently lossy** — any exception in
`_execute_agent_run` becomes terminal `failed` with no retry
(`core/scheduled_tasks.py:2441`). A durable pending queue exists **only for the
avibe platform** (`core/internal_server.py:99-179` → `messages` QUEUED rows);
all five IM platforms go straight to `dispatch_turn` with no persistence.

Also: there is **no timeout anywhere** on this path — not on `done.wait()`, not
on `gate.lock.acquire()` (`modules/agents/service.py:172`), not on the recovered-
activity emit. `_on_watch_store_done` (`:1816`) respawns a *crashed* loop but a
*hung* one is never detected.

### P5 — Pinned session bindings break permanently — FIXED (#1064, merged 2026-07-29)

Two symptoms, one contract violation. **Dangling bindings:** `/new` hard-deleted
the whole scope with no anchor filter and nothing updated
`run_definitions.session_id` — the workbench archive path reclaimed bound
definitions, the IM `/new` path did not (two teardown paths, one contract) — and
at execution time `existing` and `create_once` are identical ("a pinned
`session_id`"), so neither self-healed: the definition fired and failed forever
with `enabled=1`. **Anchor `UNIQUE` explosions:** the lookup key (backend- and
status-filtered) did not match the constraint key `(scope_id, session_anchor)`,
the IM inbound find-then-create had no `IntegrityError` catch, and the unique
index existed only in the Alembic revision — any DB born from
`metadata.create_all`, including tests, silently lacked the invariant.

Fixed per D2 (`/new` pauses bound definitions), D3 (rebind preserves the old
workdir/agent/model via the reclaim snapshot), a unique models-level index, and
the supersede mechanism for cross-backend anchor claims. §10.4–10.5 record the
load-bearing subtleties.

### P6 — Task failures are invisible

- **`run_definitions.last_status` does not exist.** It is derived per-request:
  `vibe/cli.py:1466-1471` — `last_run_at && last_error → failed`, else
  `last_run_at → succeeded`. Both source fields are overwritten every fire
  (`core/scheduled_tasks.py:798-812 mark_task_result`), so **one success erases
  three days of failure**.
- `vibe task list`'s `brief=True` payload (`vibe/cli.py:1516-1531`) **omits
  `last_error` entirely**.
- Web UI shows `task.last_error` only in the selected task's detail pane
  (`ui/src/components/workbench/HarnessPage.tsx:942-951`), never as a list badge.
- **No user-facing notification fires for a failed scheduled run, anywhere.**
  Web Push (`core/web_push_notifications.py:44`) requires a persisted workbench
  message with `platform == "avibe"`; a run that dies in
  `resolve_session_id_target` never produces one. The only side effects are the
  `agent_runs` row, a `runs.updated` SSE that refreshes an already-open page, and
  `logger.error`.

## 3. Cross-cutting constraints discovered

1. **The status vocabulary is closed and load-bearing.** `RUN_STATUS_ALIASES`
   (`storage/background.py:85-94`) = `queued|running|succeeded|failed|canceled`,
   with **no DB CHECK constraint**. At least six derived predicates key off it
   (`list_pending_callbacks`, the `"ok"` field at `:1879`, `_node_status`,
   `_filter_nodes`, `_wait_for_run_result`, `derive_session_harness_activities`),
   and the Runs UI keys three separate things off the raw value —
   `<RunStatusIcon status={run.status}>`, the `STATUS_PILL_CLASS[run.status]` CSS
   lookup, and `runStatusLabel(run.status, t)`
   (`ui/src/components/workbench/HarnessPage.tsx:1517-1528`,
   `harnessRuns.ts:49`).
   → **Do not add an `interrupted` status.** Express it as **`failed`** + an
   `error` string + `metadata.interrupt_reason`.

   **Correction (2026-07-27): this bullet claimed the UI "renders `run.status`
   raw and untranslated". It does not** — `runStatusLabel` takes the i18n `t`
   function and translates (`harnessRuns.ts:49`). The claim would have sent PR6 to
   do i18n work that is already done. The real argument is *stronger* without it
   and survives unchanged: an unknown status silently degrades in **three** places
   at once — no icon, the fallback pill class, and a missing i18n key — so the cost
   of a new status value is UI breakage in triplicate, not a missing translation.
   **Correction (2026-07-27): this line used to say `canceled`.** That was wrong
   on three counts. D1 (§7) says an interrupted run is FAILED. The
   `SETTLEMENT_TERMINAL_STATUS` contract (`core/run_settlement.py:110-127`)
   reserves `canceled` for `SETTLED_BY_STOPPED` alone — its comment is explicit
   that "the other settlements are infrastructure faults with no user intent
   behind them, so they stay `failed` and remain visible to a failure counter".
   And the scenario catalog pins it from the other side: **HFR-012**
   ("user-stopped Run settles canceled, not failed") and **HFR-037** ("a stopped
   run settles canceled rather than succeeded"). Writing `canceled` on an
   eviction would make infrastructure faults read as user cancellations and drop
   them out of the failure visibility PR6 exists to build.
2. **Use guarded writers only *for new reconcile code*.** `settle_deferred_run`,
   `record_run_output` and `settle_run_terminal` scope their UPDATEs to
   `queued|running`. `update_run_status` (`storage/background.py:1599`) is
   **unguarded** — never use it for reconcile.

   Three cautions, each of which has already cost a review round:

   - "Guarded" and "arbitrated by `_stronger_terminal_status` (`:507`)" are *not*
     the same set. `settle_run_terminal` (`:1949`) is guarded but does not call
     it; `defer_run_terminal` (`:2028`) calls it but writes no status at all,
     recording a deferred intent in `result_payload_json` and leaving the row
     `running`.
   - "Guarded" is not the same set as "terminal" either, and the guarded set is
     **not** the set of writers that reach terminal today. Two live paths
     terminalize unguarded — `complete()` via `update_run_status`
     (`core/scheduled_tasks.py:1604`) and `complete_coalesced` via
     `complete_coalesced_agent_runs_for_workbench_in_connection`
     (`storage/background.py:654`). Any rule of the form "every terminal
     transition does X" must cover those two or it does not cover ordinary
     synchronous failures at all.
   - Consequently, anything keyed on "terminal transition" must use **the status
     write** as the test — not the arbitration call, and not guardedness. See the
     PR6 notification correction for what each substitution cost.
3. **No `session_id → [run_ids]` index exists.** In-memory state is partial
   (`_inflight_executions` keyed by run id, `_inflight_sessions` a bare set,
   `SessionTurnManager.in_flight`). Any reconcile or eviction-pin must query the
   DB. Eviction holds only `composite_key = f"{base_session_id}:{working_path}"`
   (`session_handler.py:571`), so it needs a two-hop resolve via `agent_sessions`.
   The "non-terminal statuses" tuple is already redefined three times
   (`storage/workbench_sessions_service.py:46`, `core/services/session_fork.py:27`)
   — **promote one shared constant instead of adding a fourth.**
4. **P3's reconcile resolver and P4's eviction-pin provider are the same helper.**
   Build it once.
5. **`core/scheduled_tasks.py` imports no i18n at all**, yet
   `_fallback_callback_result` (`:2210-2225`) emits hardcoded English to users.
   That is an existing CLAUDE.md §6 violation at exactly the seam this work
   touches.
6. **There is no en/zh key-parity test** in `tests/`. Adding a key to one file
   only will not be caught.
7. **`evict_idle_sessions` has no behavioural test coverage at all.** That is the
   hole that let P3/P4 ship.

## 4. Proposed solution, staged

Seven PRs, ordered by risk-adjusted value. Each is independently shippable
**except PR2**, which the 2026-07-27 re-verification found to need PR6's
notification path — see §5 and PR2 correction 2.

### PR1 — P2: capture `result_text` for every harness run — MERGED (#1063, 2026-07-28)

**Merged; the implementation is the spec.** §10.1–10.3 record what
implementation corrected in the prescription that used to live here. The facts
other PRs still consume:

- `HARNESS_TRIGGER_KINDS` selects the recorder; `HARNESS_RUN_ID_TRIGGER_KINDS`
  is the subset whose `task_execution_id` *is* a run id, excluding
  `activity_recovery` (§10.1 — the widened set is two sets, not one).
- The guarded text backfill writes only when the stored terminal status
  **equals** the delivered outcome (§10.3), and does not re-transition status.
- PR7's positive settlement rides the recorder gate PR1 widened.

Scenario IDs: HFR-041 … HFR-048 (used).


### PR2 — P3: reconcile on teardown

1. `ScheduledTaskService.cancel_session_executions(session_id) -> int` — scans
   from a session id to the tasks to `task.cancel()`. **The lookup is a
   three-hop join, and the middle hop is the one that matters (corrected
   2026-07-27):** `_canonical_session_lock(session_id, …)` (`:2678`, memoized in
   `_session_lock_cache` `:1703`) gives the lock key → `_session_lock_owners`
   (`:1701`, written at `:2711`) maps that lock key to the owning run id →
   `_inflight_executions` (`:1692`) maps the run id to the task. Round 13 listed
   only the first and last of those, which does not compose: the cache is keyed by
   session id with lock keys as values and `_inflight_executions` is keyed by run
   id, so with `_session_lock_owners` omitted there is **no path from a session id
   to a run id at all** and the scan misses every ordinary pinned-session
   execution — i.e. the common case, leaving the wedge this step exists to remove
   fully intact. The new `run_id → session_id` map below covers only
   `create_per_run`, which has no lock key by design and so cannot use this join.
   Both paths are required; neither subsumes the other, and the tests must cover
   both.

   **There is a third lane, and after PR7 it is the one that matters most for
   avibe (added 2026-07-27).** Both joins above search the *scheduler* lane. An
   avibe-targeted scheduled/watch run does not stay there: `_execute_request`
   hands the turn to `gate.submit_scheduled` and returns (`:3258-3261`), so the
   outer execution leaves `_inflight_executions` while the real work continues
   under `SessionTurnManager.in_flight` (`core/session_turns.py:759`). Today that
   is survivable because the run row is already (prematurely) settled. **After PR7
   it is not**: the row stays `running` until the turn produces a terminal result,
   so a session evicted in that window has a live manager turn the scheduler scan
   cannot see, no cancel reaches it, and killing the backend leaves both the turn
   waiter and its owned run stuck indefinitely. That is the same wedge this step
   exists to remove, relocated to the lane PR7 moves the work into — and it would
   be introduced *by* PR7, silently, because PR2's tests all exercise the
   scheduler lane.

   So the teardown must cancel **both lanes**, keyed on the same session id — but
   **not** through `SessionTurnManager.cancel`.

   > **Correction (2026-07-27, same day) — `cancel(session_id)` is the user-Stop
   > API, not a generic cancel.** The previous revision called it "wiring rather
   > than new machinery". It is the wrong entry point: it sets
   > `turn.stop_no_flush` and `suppress_stop_no_active_notice` and routes through
   > `command_handler.handle_stop` (`core/session_turns.py:1721-1745`) — i.e. it
   > *is* `/stop`. Critically it never sets `Turn.cancel_settled_by`, so `_run`
   > falls back to `SETTLED_BY_STOPPED` (`:1109-1110`) and the run settles as
   > **`canceled`** — user intent — instead of `failed` with
   > `interrupt_reason=evicted`. That inverts D1 for the evicted case: an
   > infrastructure interruption would be recorded as something the user asked
   > for, and the eviction notice PR6 owes would never fire, because nothing
   > classifies it as a failure. **HFR-029** is precisely this rule ("an
   > infrastructure fault is not a cancellation") and my wiring would have
   > violated it.

   The correct shape is already in the codebase one screen away. The backend-refresh
   path records the cause on the `Turn` *before* cancelling —
   `turn.cancel_settled_by = SETTLED_BY_BACKEND_REFRESH` (`:1697`), with a comment
   stating the reason in the same terms: "this is a runtime refresh, not a user
   Stop, so a scheduled run this turn owns must not settle as `canceled` with the
   user-stop explanation". Eviction is the same class of event and takes the same
   shape: a **cause-aware manager cancellation** that sets `cancel_settled_by` to
   the eviction settlement before cancelling, so `_run` pops it and the run
   terminalizes `failed` with `interrupt_reason=evicted`. This also composes with
   step 2's `run_id → cause` map — same principle, other lane: **record the cause
   before the cancel, never infer it after.**

   The owed test must therefore assert the **reason and status**, not just that the
   run left `running`: a terminality-only assertion passes against
   `SETTLED_BY_STOPPED` and would have let this ship. PR3's pinning has the same blind spot —
   it builds pins from pending rows plus the scheduler map — so a session with a
   live *manager* turn must pin too, or eviction races the very turn PR7 is
   waiting on. Owed test:
   `test_evicting_a_session_cancels_its_workbench_turn_and_fails_the_run_as_evicted`
   — asserting `status == "failed"` and `interrupt_reason == "evicted"`, **not**
   merely that the run reached a terminal state.
   Called
   from `evict_idle_sessions` (`core/handlers/session_handler.py:1493`) on
   **both** branches, wired through `core/controller.py`.
   This is the must-have for the wedge: cancelling makes `_on_execution_done`
   release `_inflight_sessions`, so the next drain can dispatch → **the wedge is
   gone**.

   **`create_per_run` is invisible to this scan and needs an explicit association
   (added 2026-07-27).** `_execution_lock_key` returns `None` for that policy by
   design — "Returns ``None`` for ``create_per_run`` (fresh session each time)",
   `core/scheduled_tasks.py:2596-2610`. The claimed request therefore carries no
   `session_id`, and the id that `_reserve_runtime_session` (`:3117`) mints lives
   only in `_execute_task`'s local scope until the run completes, so
   `_session_lock_cache` (`:1703`, populated at `:2695`) never learns about it.
   `cancel_session_executions(session_id)` scanning `_inflight_executions` +
   `_session_lock_cache` consequently **cannot find that execution at all**: when
   the runtime-created session is evicted the task and run stay `running` forever,
   evading both the cancel and the reconcile sweep — the exact zombie class PR2
   exists to eliminate, in the one policy where nothing else can catch it.

   So PR2 must record the run↔session association **the moment reservation
   succeeds**, not at completion: a **dedicated `run_id → session_id` map**,
   populated inside `_reserve_runtime_session`'s success path and torn down in
   `_on_execution_done` alongside the existing `_inflight_sessions` release.

   It must be a new map, not `_session_lock_cache`. That cache is
   `session_id → canonical lock key` (`:1703`), and `_canonical_session_lock`
   returns any cached value verbatim as the lock key (`:2678-2681`). Writing a run
   id into it would (a) be keyed the wrong way round for a cancel that starts from
   a run id in `_inflight_executions`, and (b) hand that run id back to every later
   caller as a session lock key — silently corrupting per-session serialization for
   an unrelated request. The round-12 wording offered it as an equivalent option;
   it is not one, and the parenthetical is withdrawn. Without it the scan in this step has a permanent blind spot, and the
   DB reconcile in step 3 becomes the *only* thing that ever settles a
   `create_per_run` zombie — which is precisely the "cancel first, then reconcile"
   ordering that step 3 says should find nothing to do.

   **Correction (2026-07-27) — this step used to say the cancel "requeues the run"
   and that "semantics match the restart sweep exactly".** Both halves are now
   wrong and dangerous as an instruction. Today's `except asyncio.CancelledError`
   branch does requeue (`self.request_store.requeue(request.id)`,
   `core/scheduled_tasks.py:2821-2822`) — that is the **behaviour PR2 must
   change**, not preserve. An implementer following the old wording for service
   stop or eviction would leave the row queued, and it would be dispatched again
   after restart, repeating user-visible side effects. Semantics do still match
   the restart sweep, but because **both terminalize** (D1 correction), not
   because both requeue. Step 1 delivers the cancel; step 2 replaces what the
   cancel handler does.
2. Per **D1**, the cancelled run is **terminalized, not requeued**:
   `defer_run_terminal` → `settle_deferred_run` with
   `metadata.interrupt_reason`. This also removes the
   eviction↔requeue storm hazard, so no attempt counter is needed. Concretely,
   `_execute_claimed_request`'s `except asyncio.CancelledError` branch
   (`core/scheduled_tasks.py:2821-2822`) stops requeueing and terminalizes
   **every claimed request, with no per-type allowlist.**

   **The three-type list was an incomplete allowlist (corrected 2026-07-27).** It
   read "`scheduled`, `agent_run`, `watch`, with only `watch_runtime` exempt",
   which is not the same set and is missing two. `list_pending` admits six request
   types — `task_run`, `hook_send`, `agent_run`, `scheduled`, `watch`, `webhook`
   (`core/scheduled_tasks.py:1084`) — and `_execute_claimed_request` dispatches
   `hook_send` / `watch` / `webhook` through **one** branch to the same
   `_execute_request` (`:2763-2786`), so a `hook_send` or a `webhook` is an agent
   turn with arbitrary prompt and arbitrary side effects, indistinguishable from a
   `watch` at the point of cancellation. Implemented from the old list, eviction
   or shutdown would keep requeueing exactly those two, re-running their prompts
   and repeating posts and tool calls after restart — the storm this step exists
   to remove, left in place for the types nobody enumerated.

   **The rule is the specification; the enumeration is only evidence.** Anything
   `_execute_claimed_request` was handed has already been claimed, and
   `watch_runtime` never reaches it — `list_pending` does not admit that
   request_type at all (`:1084`), so the "exemption" was never a case the handler
   could see and stating it as one invited an allowlist. So: **terminalize the run
   the handler holds.** No type test, and a request type added later is covered on
   the day it is added rather than on the day someone remembers this paragraph.

   > Deliberately the opposite structure from the terminal-writer table above,
   > and for a reason. There, a *behaviour* had to be verified writer by writer,
   > so the enumeration is normative. Here every claimed type reaches one handler
   > through one branch, so the enumeration is a fact about today's tree and the
   > rule is what an implementer follows. Round 31's lesson was that a proxy
   > predicate must not stand in for the real condition; this round's is the
   > converse — an enumeration must not stand in for a rule that is already
   > exactly stateable.

   **The reason cannot be hard-coded, because `CancelledError` does not carry one
   (corrected 2026-07-27).** This step used to say
   `metadata.interrupt_reason = "evicted"` outright. Every cancellation source
   funnels through the same undifferentiated `task.cancel()` and lands in the same
   single handler: eviction (step 1), service shutdown
   (`_begin_stop:1968-1970`, reached from `stop()` and from the lost-lease path
   `_owns_service_instance:1982`), and the per-run lifetime cap this plan adds
   later (§"Per-run lifetime cap", which requires `lifetime_timeout`). Correction 1
   below separately requires `restarted` for the shutdown case. So a hard-coded
   `"evicted"` mislabels **two of the three** sources — a run killed by a deploy
   would tell the user they were evicted for idleness, and the lifetime cap could
   never surface its own reason at all. That defeats the point of the field:
   `interrupt_reason` selects the user-facing copy (§PR6) and orders settlement
   precedence (HFR-023).

   So PR2 must **record the cause before cancelling, not infer it after**: a
   `run_id → cause` map (or a `cause` argument threaded through a small
   `_cancel_execution(run_id, cause)` helper that every cancel site calls instead
   of `task.cancel()` directly) written immediately before the `cancel()`, read by
   the `CancelledError` handler, and cleared in `_on_execution_done`. `stop()`
   already clears `_inflight_executions` / `_inflight_sessions` /
   `_session_lock_owners` at `:2004-2006`, so the new map is cleared in the same
   place. Default when no cause was recorded — an external cancellation this plan
   did not originate — is the generic interrupted reason, **not** `evicted`;
   guessing is what this correction exists to stop. *(Corrected 2026-07-27: this used to say "branch on the
   trigger's idempotency". That rule was retracted — `_enqueue_hook`
   (`core/watches.py:1301-1324`) dispatches an arbitrary agent prompt under
   `run_type="watch"`, so no trigger-idempotency allowlist is safe. See correction
   1 below.)*
3. A DB reconcile sweep for runs the cancel didn't reach, using guarded writers
   only. Order matters: **cancel first, then reconcile** — the happy case finds
   nothing to do. This settles the run row; it does **not** by itself notify the
   user — see the notification correction below.
4. Promote the shared non-terminal-status constant (§3.3).
5. i18n the interrupted copy.

**These are PR2 scope, not a follow-up (corrected 2026-07-27).** An earlier
revision listed the other run-blind teardown paths — Running-tab "End",
controller shutdown, Codex/OpenCode teardown — as a "follow-up in the same PR
family", which assigned them to nothing: no PR number, no dependency, no owner.
The P3 matrix already identifies all of them as run-blind and D1 applies to
teardown generally, so leaving them unassigned means that after all seven PRs
ship, killing a backend from the Running tab still bypasses the shared
cancellation hook — the row and its session lock stay wedged, or the
interruption stays silent. That is the original defect, surviving the plan that
was written to remove it.

So PR2 lands the shared teardown helper and routes **every run-blind path** in the
P3 matrix through it, not `evict_idle_sessions` alone; per CLAUDE.md §2 the
reconcile belongs on the helper by construction.

**"Every path" was too wide, and Running-tab End is the counter-example
(corrected 2026-07-27).** The revision above said *every* matrix path, listing End
among the run-blind ones. It is not, on the branch that matters. For an active row
End goes through `_stop_active_agent` (`core/services/running_agents.py:1182`,
`:955-1003`): it tries `_settle_workbench_turn`, then the canonical
`command_handler.handle_stop` with the live sink bound via
`bind_context_to_turn_sink`, then `settle_bound_turn_sink`, which records
`SETTLED_BY_STOPPED` — and `SETTLEMENT_TERMINAL_STATUS` maps that to **`canceled`**
(`core/run_settlement.py:110-127`). The run-blind helpers the old matrix row cited
(`:595-727`) are reached only on the *non-active* branch (`:1224-1228`), where there
is no turn to settle; master already widened "active" to include the pending-start
window so that branch cannot swallow a live turn (`:220-232`).

Routing End through PR2's interruption helper would therefore rewrite an explicit
user action as `failed` with an `interrupt_reason`, and fire the D1 interruption
notice for a button the user just pressed. That is **HFR-012** and **HFR-037**
inverted, and §3.3 above already reserves `canceled` for `SETTLED_BY_STOPPED`
alone.

> **One rule, and this document has now broken it in both directions.** The §3.3
> correction caught the reverse error — wiring eviction through
> `SessionTurnManager.cancel`, the user-Stop API, would have recorded an
> infrastructure fault as user intent (**HFR-029**). This paragraph did the mirror
> image 170 lines later: it recorded user intent as an infrastructure fault. The
> rule is symmetric and belongs stated once: **the cause is recorded at the cancel
> site and never inferred from the path.** The shared helper therefore does not
> decide status or copy — it *carries the cause*, and
> `SETTLEMENT_TERMINAL_STATUS` decides the terminal status while
> `SETTLEMENT_I18N_KEYS` decides the text (`core/run_settlement.py:98-127`).
> User-intent causes settle `canceled` and fire **no** interruption notice; the
> infrastructure causes settle `failed` and do. Anything else means the helper's
> caller list is the specification, which is what both errors were.

Consequences for PR2's scope, stated so the narrowing does not read as a
loophole. End's non-active branch is genuinely run-blind, and has no live run *at
the moment of the state read* — which is not the same as having none by
construction.

> **The residual was handed to a sweep that cannot accept it (corrected
> 2026-07-27).** The paragraph above said the race where a run acquires the turn
> after the state read "is covered by step 3's reconcile sweep rather than by new
> machinery". It is not, and this document says so forty lines below in the
> Post-#1005 note: **`sweep_stale_runs` exempts `owned_run_ids`**
> (`storage/background.py:2412-2420`, `if run_id in owned_run_ids: continue`).
> A run that won the turn is owned by a live manager turn, which is precisely the
> condition the sweep declines to touch — and declines *deliberately*: the comment
> there reads "A live owner outranks every TTL, whatever the row's status… a
> turn-duration timeout by the back door, which this design explicitly does not
> have" (`:2413-2419`). So the delegation is not merely optimistic; it asks for
> the one behaviour the sweep exists to refuse. Left as written, the run stays
> `running` with a torn-down backend until something else settles it, which is
> **HFR-001**, the ghost this PR is for.

The fix is not a narrower window. **End's settlement is unconditional, not
state-dependent** — the same shape as the §3.3 rule above, applied to the read
instead of the write:

1. Always invoke the cause-aware cancellation with cause `stopped`, whatever the
   state read returned. There is no branch to race, so there is no residual. It is
   silent when nothing was active: `_build_session_row_stop_context` sets
   `suppress_stop_no_active_notice = True` (`core/services/running_agents.py:933`)
   and `command_handlers.py:1060` honors it, so the idle case costs one no-op call
   and no user-visible message.
2. Then tear down the backend regardless of what the stop returned. That is
   master's own precedent on this endpoint, not a new invention — the codex branch
   already tears down "even when the stop FAILED" (`:1195-1197`). Generalizing it
   to every branch is what makes step 1 safe to call unconditionally.

Ordering matters and is the whole point: settle first, tear down second. The
reverse order is the bug being fixed, since a torn-down backend can no longer
settle its own turn.

Owed tests:
`test_ending_an_active_row_settles_the_run_canceled_with_no_interruption_notice`,
asserting the status **and** the absence of a notice, since a terminality-only
assertion passes against either outcome — the same blind spot the §3.3 owed test
was written to close; and
`test_ending_an_idle_row_is_silent_and_settles_nothing`, which is what pins the
unconditional call as *safe* rather than merely correct. Where a backend's teardown
cannot be reached in PR2 (Codex/OpenCode may need their own transport work),
that path gets an explicit staged PR number and a dependency edge rather than
prose — an unassigned path is indistinguishable from a forgotten one, which is
what this correction is.

**Post-#1005 note (2026-07-27).** Neither safety net that landed with #1005 can
substitute for this PR, and both fail for the same reason: `sweep_stale_runs`
exempts `owned_run_ids`, and `_release_leaked_session_locks` classifies a lock as
leaked only when `run_id not in self._inflight_executions`. An evicted-but-
uncancelled execution stays in `_inflight_executions` permanently, so it is
"owned" by both tests forever. Step 1 (cancel the execution task) is therefore not
just the cheapest half of this PR — it is what makes the already-landed sweep
reach this case at all. Extend `core/run_settlement.py` with `evicted`, which
`:14` already reserves for this plan.

**Correction 1 (2026-07-27) — service stop must terminalize too.** Step 2 above
originally carved out `stop()` as a path that "should still requeue". That
contradicts D1, which names **teardown** alongside eviction, and it is a live
duplicate-prompt hazard rather than a stylistic inconsistency:

- `stop()` (`:1985-2006`) cancels every entry in `_inflight_executions`, and its
  own comment states the intent — "Cancellation is caught by
  `_execute_claimed_request`, which requeues the run, so it is picked up again on
  the next start".
- `_execute_claimed_request`'s handler (`:2821-2824`) is
  `except asyncio.CancelledError: self.request_store.requeue(request.id)` with **no
  discrimination at all** — not by trigger kind, not by cause.
- The D1 restart backstop does not cover the result. `recover_processing_runs`
  terminalizes `running|processing` rows; a run requeued by `stop()` is sitting at
  `queued`, so on the next start it is dispatched again. A daily report
  interrupted mid-turn posts twice — precisely the outcome D1 exists to prevent.

The branch condition is therefore **not the cancel origin**. It is also **not a
trigger-kind allowlist** — an earlier revision of this paragraph proposed keeping
requeue for `watch`, which is wrong: `_enqueue_hook` (`core/watches.py:1301-1324`)
enqueues an arbitrary agent prompt as `run_type="watch"`, so a watch run has the
same side effects as a scheduled one. The rule is simply:

> **Terminalize every claimed request the handler holds, with no `request_type`
> inspection** — `metadata.interrupt_reason ∈ {evicted, restarted}`. Today that
> set is `task_run`, `hook_send`, `agent_run`, `scheduled`, `watch`, `webhook`
> (`list_pending`, `core/scheduled_tasks.py:1084`) — every one a claimed agent
> turn with arbitrary side effects — and the enumeration is evidence, not the
> rule: a request type added later is covered on the day it is added. (An
> earlier revision of this rule enumerated only three types, which was an
> incomplete allowlist an implementation could copy — step 2 above has the full
> derivation.) `watch_runtime` never reaches the handler at all — `list_pending`
> does not admit it — and `recover_processing_runs` already excludes it at
> `storage/background.py:2202`.

Concretely the `except asyncio.CancelledError` handler stops calling
`request_store.requeue(request.id)` and settles instead; no `request_type`
inspection is needed on that path, which makes it *simpler* than the current code
rather than more conditional.

**Correction 2 (2026-07-27) — the callback does not come free.** Step 3 claimed
the D1 notification arrives via the callback path. It does not, for the two
largest classes of interrupted run:

- `enqueue_task_run` → `enqueue_definition_run` (`:923-975`) never sets
  `callback_session_id` on the row.
- `list_pending_callbacks` (`storage/background.py:1521-1536`) filters on
  `callback_session_id IS NOT NULL AND != ''`.

So an ordinary `scheduled` run — and any agent run created with an explicit
no-callback policy — can be terminalized with `interrupt_reason=evicted` and the
user is **never told**, which is the exact silent-failure D1 forbids. Two ways
out, and the plan must pick one before PR2 ships:

- **(a) Order PR6 first** and route interruption notifications through the
  notification path it builds. Costs a dependency edge PR2 did not have.
- **(b) Give PR2 its own minimal notification** for interrupted runs with no
  callback target, resolving the destination from `post_to`/`deliver_key` on the
  run row.

(a) is preferred: PR6 has to build actionable-failure delivery anyway, and (b)
would be thrown away when it lands. Either way **PR2's exit criterion is a
delivered user-visible notice, not a terminal row** — which is precisely why the
drain that delivers it must be a *prerequisite* of PR2 rather than later work; see
the §5 ownership correction.

**Refinement (2026-07-27, from the PR7 restart correction).** "Order PR6 first"
is necessary but not sufficient, because PR6 hooks `_execute_task` and two of the
three interruption paths never reach it — restart terminalizes from
`__init__`, and eviction terminalizes out of band. The mechanism that actually
works for all three is the **owed-notice stamp** described under PR7:
`metadata.owed_failure_notice`, written by the same guarded UPDATE that
terminalizes, drained on the existing 2 s tick. PR2 should stamp it rather than
grow a delivery of its own; **PR6 owns both the drain and the rendering** (§5
ownership correction — the drain cannot be PR7's, or PR2 would ship stamping
notices before anything could deliver them). That keeps one drain instead of three
call sites and makes the dependency "PR2 needs the stamp + PR6's drain", not
"PR2 needs to be reordered behind PR6's `_execute_task` hook". The stamp is a
`pending`/`sent` state acknowledged on **either** valid evidence of delivery — a
persisted `messages` row, or a returned message id whose row write failed
(`ack_evidence="delivery_only"`) — never on a bare function return, and
deduplicated by a stable run-derived `failure_id`. Both signals count precisely
so an already-delivered notice is never resent; see the acknowledgement protocol
under PR7 for the full outcome table, the bounded retry that applies only when
there is *no* evidence of delivery, and the at-least-once guarantee this does and
does not provide.

### PR3 — P4a: eviction interlock; liveness only on real progress

1. **Pin provider:** `pinned_composite_keys()` built from
   `request_store.list_pending()` + `_inflight_executions`, consumed by
   `evict_idle_sessions` (`session_handler.py:1441-1454`). Must be recomputed
   **inside the second recheck pass** or the existing two-pass structure
   reintroduces the hole. Failure semantics are two-level (corrected
   2026-07-29, review): an **individually unresolvable `session_id` fails
   open** — that binding does not pin, or a dangling P5 binding creates an
   immortal session — but a **provider-level failure fails closed**: if the
   provider itself raises (SQLite outage, resolver error), it cannot report
   *any* pending or in-flight ownership, and evicting on missing safety data
   would tear down sessions whose queued work is valid. On a systemic provider
   error the eviction pass aborts for that cycle and retries on the next sweep
   (cadence 100 s); only IDs that resolve successfully *as absent* are treated
   as non-pinning. Reuses PR2's
   resolver (§3.4). Per **D4**, the pin is **time-bounded** at
   `stuck_active_floor_seconds` (1800s); past that, evict **and** reconcile.
2. **No liveness touch at claim (corrected 2026-07-29, review).** An earlier
   revision had `_spawn_execution` (`core/scheduled_tasks.py:2705`) call
   `touch_session_activity(composite_key)` when a request was claimed. A claim
   is not progress: `_spawn_execution` runs before the turn necessarily starts
   or clears the session gate, so with a hung manager turn every recurring
   successor is parked behind the gate having produced no assistant or tool
   output — and a definition firing more often than the 1800 s ceiling would
   refresh the exact clock stuck-session eviction reads, making the hung
   session immortal. That is the fake-activity defect of the gate-wait
   heartbeat below, one call site earlier, and it fails the same bar: bump
   `last_activity` only where observable turn progress occurred. Queued and
   blocked claims are item 1's job — the pin, which is time-bounded by
   design. If a claim-recency signal is ever wanted, it must be a separate
   timestamp that eviction does not treat as liveness. The inbound-message
   touch after `get_session_info` (`message_handler.py:169`) stays: an inbound
   user message is real activity. (Enqueue could never touch anyway —
   different process, in-memory dict.)

   **Correction (2026-07-27) — the gate-wait heartbeat is removed, and it would
   have made hung sessions immortal.** This step used to add a timer that touched
   `session_last_activity` while blocked on `gate.lock`
   (`modules/agents/service.py:172`). But `evict_idle_sessions` iterates
   `self.session_last_activity` and derives **both** the ordinary idle time and
   the stuck-active threshold from that same value
   (`core/handlers/session_handler.py:1493-1533`). Refreshing it on a timer
   therefore resets the exact clock the ceiling is measured against, so a hung
   turn never reaches `stuck_active_floor_seconds` — item 1's own 1800 s bound
   becomes unreachable, the session is never force-evicted, and every successor
   waiting on the gate blocks indefinitely. The two items of this PR contradicted
   each other.

   Two reasons the heartbeat is unnecessary, not merely harmful:

   - **The pin already covers the case it was for.** A waiting successor's
     purpose was to keep the target session alive; item 1's pin provider does
     that, and does it *time-bounded* by design. The heartbeat added nothing but
     an unbounded escape hatch.
   - **Waiting is not activity.** The docstring at `:1507-1516` is explicit that
     `last_activity` is bumped by real assistant/tool traffic, which is what
     makes "older than the cap" a meaningful stuck signal. A session blocked on
     `gate.lock` is doing no work; counting that as activity is what breaks the
     signal.

   **Retraction (2026-07-27, same day) — do NOT move the ceiling onto
   `session_turn_started`.** The revision immediately above proposed exactly that,
   and it was a worse bug than the one it fixed. `session_turn_started`
   (`core/controller.py:205`) measures absolute turn age, so a ceiling on it
   force-evicts a **healthy** turn that has simply run past 1800 s while streaming
   assistant/tool events the whole way. That is a production-visible 30-minute
   turn-duration timeout, and it contradicts an explicit, documented invariant:
   `dispatch_turn_with_outcome` states "There is **NO** turn-duration timeout: an
   agent turn may legitimately run for hours, and the controller must never kill it
   on a timer" (`core/services/dispatch.py:118-121`) — the same invariant this plan
   already cites in D4 as the reason a lifetime cap is needed elsewhere.

   `last_activity` is the **right** clock and was never the defect. It exists to
   distinguish a silently stuck turn from an actively streaming one, which is
   precisely the discrimination the ceiling needs. The actual bug was narrower: a
   gate-wait heartbeat manufactures *fake* activity for a session that is doing no
   work, so it corrupts a signal that is otherwise accurate. **Removing the
   heartbeat is the whole fix.** Any future touch must clear the same bar — bump
   `last_activity` only where real progress occurred — rather than the ceiling
   being re-based to defend against touches that should not exist.

Neither changes what a Run means, so PR3 does not collide with PR7.

### PR4 — P4b: unstall and supervise the drain loop (own review)

This is the actual root cause of the 65-minute outage and the reason a restart
was needed. **PR3 does not prevent a recurrence** — it only stops the outage from
also destroying the target session.

- Move `_drain_recovered_activity_outputs` / `_drain_callbacks` /
  `_drain_vault_callbacks` off `_watch_store`'s critical path (own tasks, or
  `asyncio.wait_for`-bounded); timeout `emit_agent_message` at `:1753` and
  requeue via `registry.requeue_completed_output` at `:1781` — **but only under
  the delivery-evidence protocol below. Do not implement a bare requeue.**

  **The "own tasks" branch does not escape the bound (corrected 2026-07-29,
  review).** Detaching the drains without the `wait_for` bound only relocates
  the stall: a hung `emit_agent_message` keeps the detached task alive
  indefinitely, its claim held, and each later tick can pile another stuck
  delivery on top — the 65-minute global stall becomes an unbounded
  task-and-claim leak instead of an outage. Whichever branch is taken, a
  detached drain must be **tracked** (owned by the service, cancelled at
  `stop()`), **single-flight** per drain (a tick that finds the previous
  instance still running skips rather than stacking), **time-bounded** by the
  same `wait_for` the inline branch gets, and its timeouts reconciled through
  the same delivery-evidence protocol below. "Own tasks" changes where the
  bound lives, not whether it exists.

  **A timeout is not evidence of non-delivery (corrected 2026-07-27).**
  `emit_agent_message` delivers, *then* persists, *then* streams (see its
  contract at `core/message_dispatcher.py:1371-1407`). So `asyncio.wait_for` can
  cancel it after the transport send has already succeeded but before the
  message row is written. Requeueing on that timeout re-posts a completion the
  user has already seen, and the stable-output-id check cannot suppress it
  precisely because the persisted receipt is the thing that is missing. Note this
  is the same hazard §4 spends five rounds on — inferring an outcome from the
  absence of a record that the failure itself prevented from being written — and
  I did not apply that rule here when it was derived, because this section was
  already written and I only re-checked the section under review.

  **Decided, so PR4 is implementable (2026-07-27).** An earlier revision left two
  mechanisms open while the bullet above still said "requeue" and the header
  marked PR4 approved — the same defect as the PR7 header, created in the commit
  that fixed the PR7 header. Leaving a choice open is only honest if nothing else
  in the document tells an implementer which branch to take.

  The protocol: `emit_agent_message` records a durable **`sending`** transition
  for the output id *before* the transport call, and on return records the
  delivered id in the same write that persists the message row, clearing
  `sending`. The drain then reads three states — no marker (never attempted,
  requeue), `sending` with no delivered id (**ambiguous**), delivered id present
  (done, drop). This does not remove the ambiguous state; nothing short of
  transport-level receipts can. It makes it *nameable*, which is the part the
  previous revision was missing.

  For the ambiguous state, **requeue**. Unlike PR7's claimed row this is a
  decision I am comfortable recording rather than escalating, because the
  asymmetry is not close: the payload is a completion notice, so a duplicate is
  cosmetic and self-evident to the user, while a drop means they never learn the
  watch fired — and the watch firing is the entire product. PR7's payload is an
  agent execution, where a duplicate re-runs tools and spends money; that is why
  that one is a maintainer decision and this one is not. The `sending` marker
  still earns its cost by collapsing the common case (crash before the transport
  call) into a clean requeue and the settled case into a clean drop, so
  duplicates are confined to a genuinely narrow window rather than produced by
  every timeout. Maintainers who disagree can invert the ambiguous branch without
  touching the mechanism.

  **The ambiguous requeue must be bounded, or "narrow window" is false
  (corrected 2026-07-27).** If the transport keeps succeeding while persistence
  or streaming keeps timing out, every attempt lands back in
  `sending`-with-no-delivered-id and the rule above requeues it again — posting
  another copy each round, indefinitely. That is not a narrow window; it is a
  duplicate generator whose rate is set by the retry interval, and the claim
  above was wrong without this bound.

  So the marker carries `attempts` and `next_attempt_at`, and the ambiguous
  branch retries **at most once**: first ambiguous timeout requeues, a second on
  the same output id stops and dead-letters to the owed-failure-notice drain
  rather than re-sending. The asymmetry argued above still holds for the *first*
  retry — a possible duplicate notice beats a certain silent drop — but it does
  not extend to unbounded retries, where the expected number of duplicates grows
  without limit while the chance the original was actually lost does not.
  Repeated persistence failure is itself the thing the user needs told about, and
  the drain is what tells them.

  **The dead letter needs an owner that exists (corrected 2026-07-27).**
  "Dead-letters to the owed-failure-notice drain" assumed every Activity has a
  run row to hang the notice on. It does not. `SessionActivity.run_id` is
  optional (`core/session_activities.py:47`), and `_activity_run_ids` returns
  `{str(activity.run_id)} if activity.run_id else set()` plus any metadata
  `run_ids` (`:193-198`), so for a recovered Activity created by an ordinary chat
  turn the set is legitimately empty. PR6's drain scans `agent_runs.metadata`;
  with no run row there is nothing to stamp, and the second ambiguous timeout
  would stop the resend while writing the notice nowhere — silently dropping the
  completion, and leaving the Activity claimed forever because nothing ever
  reaches the state that releases it. That is the delivery-evidence hazard again:
  stopping the retry destroys the last evidence before delivery is confirmed.

  So the dead letter is **Activity-owned, not run-owned.** The durable record is
  the same per-output-id marker that already carries `attempts` /
  `next_attempt_at`; the second ambiguous timeout moves it to `failed` and stores
  the error, which requires no run row to exist. Where a run *does* exist the
  marker cross-links to it (`run_id` / `task_execution_id`) so the run-scoped
  drain finds the same dead letter rather than a second, divergent one — one
  record, two indexes, never two records.

  The terminal transition must be written down too, because the previous revision
  named the failure state and not the exit from it. On delivery of the notice —
  and only on confirmed delivery, per the rule this section keeps relearning —
  the marker moves `failed` → `acknowledged`, and *that* transition releases the
  Activity from claimed state and settles the deferred run when there is one.
  Not the send attempt, not the stamp.

  **But "only acknowledgement retires it" was unbounded, and unbounded is the
  defect this PR removes (corrected 2026-07-29, review).** If delivery of the
  dead-letter notice itself fails persistently — the transport stays down — a
  rule that re-picks the `failed` marker on every tick until acknowledgement
  never terminates: the Activity stays claimed forever and the drain carries
  the retry forever, which is PR4's own unbounded-work hazard rebuilt inside
  its fix, and it contradicts PR6's bounded-retry/dead-letter contract. So the
  notice attempt gets its own bounded budget — the same
  `attempts`/`next_attempt_at` backoff protocol as every other notice, counted
  separately from the delivery attempts that produced the dead letter. On
  exhaustion the marker moves `failed` → **`abandoned`**: a terminal, durable,
  operator-visible state (listed wherever dead letters surface — `vibe task
  show` / run detail) that **releases the Activity claim and settles the
  deferred run** with the stored error, exactly as acknowledgement would,
  while recording that the user was never told. `abandoned` is the honest name
  for "we gave up telling them"; keeping the Activity claimed instead would
  not tell them either — it would only wedge the Activity and hide the
  giving-up.

  Owed: `test_activity_dead_letter_without_run_row_is_recorded_and_drained`,
  `test_activity_dead_letter_acknowledgement_releases_claim_and_settles_run`,
  `test_notice_retry_exhaustion_abandons_the_marker_and_releases_the_claim`,
  `test_timeout_after_transport_send_requeues_at_most_once`,
  `test_consecutive_post_send_timeouts_stop_after_one_retry` (the case the
  single-timeout test cannot establish), and
  `test_timeout_before_transport_send_requeues_cleanly`.
- Per-tick heartbeat timestamp + watchdog that forces `_drain_dirty=True` and
  logs loudly when a tick is overdue. This alone makes the next occurrence
  self-diagnosing and costs nothing.
- Re-arm `_drain_dirty` on the skip branches at `:2021/2023/2026/2032` so a skip
  always guarantees a retry — **with backoff**, or a permanently-unrunnable
  request hot-spins at 2s.

**Open design conflict — settle this before writing code (added 2026-07-27).**
`core/services/dispatch.py` states the opposite invariant in a comment that
predates this plan:

> There is NO turn-duration timeout: an agent turn may legitimately run for hours,
> and the controller must never kill it on a timer.

`agent-run-zombie-settlement.md` §2 reaffirms it by listing "turn-duration timeout"
as an explicit non-goal, citing the same source. The distinction this PR must
defend is that it does **not** propose bounding a turn: it proposes bounding the
*service loop that delivers already-completed output*. `emit_agent_message` inside
`_drain_recovered_activity_outputs` is post-turn delivery, not turn execution, and
a hang there stalls every other tenant of `_watch_store`.

**Settled — the distinction holds, and PR4 is implementation-ready
(2026-07-27).** This block previously ended "if that distinction does not survive
review, PR4 reduces to its two uncontested halves", which left the scope of P4b
undecided while the header approved PR4 and the delivery mechanism above was
already specified. Two readings, two materially different fixes — the same defect
as the PR7 header and the PR4 delivery bullet, now for the third time in this
plan, which is why it is being closed rather than re-flagged.

The distinction is sound on its own terms: the no-turn-timeout invariant protects
*turn execution*, and `_drain_recovered_activity_outputs` executes no turn. It
delivers output belonging to a run that has already reached a terminal state. A
bound there cannot truncate an agent's work, because there is no agent work left
to truncate — only a send that has stopped making progress while holding a shared
loop. Refusing to bound it does not protect any turn; it only lets one stuck
delivery stall every other tenant, which is the 65-minute outage this PR exists
to prevent.

So PR4 ships whole. The heartbeat/watchdog and the `_drain_dirty` re-arm remain
its uncontested halves, but they are no longer an alternative scope — if a
reviewer rejects the bound during PR4's own review, that is a normal review
outcome to argue there, not a fork the plan keeps open. A documented alternative
is an instruction to whoever reads that paragraph first.

Re-verification note: the drain block *does* now re-arm on exception
(`except Exception: self._drain_dirty = True; raise`), but the `_drain_requests`
skip branches still do not. **The three are not symmetric, and an earlier
revision grouped them as if they were (corrected 2026-07-27):** the
transport-unavailable and session-busy branches call `record_skip_reason` and
`continue` (`core/scheduled_tasks.py:2384-2399`), but the capacity branch is a
bare `break` that records **no** reason at all and ends the tick outright
(`:2376-2379`). So capacity is the stronger failure mode, not a third instance of
the same one: every remaining queued row is abandoned for the tick with nothing
written explaining why, which also means those rows never become sweepable —
`record_skip_reason` is what the sweep reads. That half of the bullet above is
still open.

### PR5 — P5: stop orphaning sessions, harden reservation — MERGED (#1064, 2026-07-29)

**Merged; the implementation is the spec.** §10.4–10.5 record what
implementation corrected in this section and remain authoritative.

Make the anchor index unique, reclaim definitions bound to a session row before
any hard delete, and snapshot the session's settings so a later rebind restores
model/agent instead of silently falling back to scope defaults. `/new` **pauses**
bound definitions (D2) rather than deleting them, and a `create_once` rebind
**preserves** the old workdir / agent / model (D3).

When another backend claims an anchor whose row already carries a native session
id, the row cannot be relabelled — the native id is write-once and
backend-specific. It is **superseded**: its anchor is moved aside so the slot
frees, and nothing is deleted.

Two properties of that move are load-bearing and were not obvious from the call
site — see §10.4. The marker's shape decides both whether definitions pinned to
the row keep their delivery thread, and whether a `/new` prefix clear
hard-deletes the row that supersede promised to keep. Any change to the marker
format must be re-checked against both.

Scenario IDs: HFR-049 … HFR-059 and overflow HFR-240 … HFR-279 (all used —
see §10.7 for the range reallocation this forced).


### PR6 — P6: make failure visible — IN REVIEW (#1072)

1. **Notify once per failure transition**, at a single choke point — but **not
   `_execute_task`** (corrected 2026-07-27). An earlier revision named
   `_execute_task:2513-2517` as that point. Only `task` requests reach it:
   `_execute_claimed_request` routes `watch`, `hook_send` and `webhook` through
   `_execute_request` instead (`core/scheduled_tasks.py:2763-2786`), and
   `agent_run` through its own branch. Choking notification there would have left
   every watch failure silent — contradicting this plan's own §8 requirement to
   "apply PR6 at the shared layer, not the task path only", which is two hundred
   lines below the sentence that violated it.

   The choke point is therefore the **common claimed-request/result layer** in
   `_execute_claimed_request`, after the per-type branch sets `error`, so one
   path covers every harness run type — **except the gated lane, which that
   layer cannot see (corrected 2026-07-27).** For an Avibe-targeted scheduled or
   watch run, `_execute_request` returns `None` immediately after
   `gate.submit_scheduled` (`core/scheduled_tasks.py:3258-3261`); the claimed
   layer observes `error = None` and a later backend failure is visible only to
   the outbound terminal recorder. Choking there alone would have left every
   Workbench-targeted failure silent, and PR7 makes that split permanent by
   moving settlement out of band — so the gap would widen rather than close.

   > This is the **third** design in this plan broken by that early return: the
   > D4 lifetime cap, the gate-parked follower in §4, and now PR6's notification.
   > The structural fact, stated once here so the next design does not rediscover
   > it: *for Avibe targets the scheduler's return value carries no outcome*, and
   > anything keyed on it is blind to the lane that PR7 makes primary. Any new
   > mechanism in this plan must say what it does on the gated path before it is
   > considered specified.

   So the notification is stamped at the shared terminal-settlement layer — not
   at the claimed-request layer, and **not at the outbound recorder alone
   (corrected 2026-07-27).** The previous revision named that recorder as "the
   shared authoritative terminal writer" and reasoned that one writer owning
   terminal state is the only arrangement in which settlement and notice cannot
   disagree. The reasoning was right; the premise was false. There are **two**
   terminal writers. A gated Workbench turn that ends without emitting a terminal
   result — an OpenCode failure path that emits only a `notify` and then calls
   `mark_turn_complete` — is settled by `_settle_turn_owned_agent_runs`
   (`core/session_turns.py:797`, invoked at `:1128`) via
   `settle_agent_runs_without_result` (`core/scheduled_tasks.py:3038`), never by
   the recorder. The claimed layer has already seen `None`, so that failed row
   would be stamped by nothing at all — precisely the "every first failure
   transition" requirement this sub-step exists to satisfy, failed on the path
   most likely to produce a silent failure.

   The fix is to stop naming call sites and use the property instead: **the
   notice is stamped by whichever UPDATE actually transitions
   `agent_runs.status` to a terminal value.** Stated as a property rather than a
   list, so a new settlement path inherits the notice instead of having to
   remember it.

   **The property is not "guarded" (corrected 2026-07-27).** The revision
   immediately below restricted the set to the *guarded* writers, and that
   excluded the single most common failure of all: when `_execute_claimed_request`
   catches an exception from a `task` / `watch` / `hook_send` / `webhook`, its
   `finally` calls `request_store.complete()` (`core/scheduled_tasks.py:2840`),
   which terminalizes through the **unguarded** `update_run_status`
   (`:1604` → `storage/background.py:1599`). An ordinary synchronous first
   failure would therefore carry no `owed_failure_notice` and the drain could
   neither notify nor retry it. Guardedness is a property of *how* the write
   races, not of *whether* it is terminal; only the latter is the test here.

   Enumerated against master, the terminal-status writers are **five**:

   | writer | transitions status? | guarded? | stamps notice |
   |---|---|---|---|
   | `record_run_output` (`storage/background.py:1816`) | yes — UPDATE scoped to `queued\|running` | yes | **yes** |
   | `settle_run_terminal` (`:1949`) | yes — the result-less settlement writer | yes | **yes** |
   | `settle_deferred_run` (`:2088`) | yes — writes `"status"` from the deferred intent | yes | **yes** |
   | `update_run_status` (`:1599`), via `TaskExecutionStore.complete` (`core/scheduled_tasks.py:1593`, called at `:2840`) | yes — the ordinary claimed-request completion | **no** — UPDATE has no status predicate | **yes** |
   | `complete_coalesced_agent_runs_for_workbench_in_connection` (`storage/background.py:654`), via `complete_coalesced` (`core/scheduled_tasks.py:1642`, called at `:2833`) | yes — per-row UPDATE at `:691` | **no** — honors `cancel_requested` but has no status predicate and no already-terminal skip | **yes** |

   The last two are also a **pre-existing clobber hazard that PR6 should fix
   while it is here**, not merely two more places to stamp. Neither scopes its
   UPDATE to `queued|running`, so a terminal status another actor already wrote
   is overwritten: a `record_run_output` success that landed first becomes
   `failed`, and for the coalesced writer a row already `succeeded` is rewritten
   wholesale. Master's own docstring on the private helper
   `_settle_agent_run_without_result` (`core/scheduled_tasks.py:3071`), which
   `settle_agent_runs_without_result` (`:3038`) calls,
   names exactly this about the `update_run_status`
   path, so it is a known sharp edge rather than a new claim. **Preferred
   prescription:** route both through guarded writers — extending
   `settle_run_terminal` to carry the identity columns `complete()` sets today
   (`task_id`, `session_key`, `session_id`) and to accept `succeeded`, and giving
   the coalesced helper the same `queued|running` predicate — so the writer set
   becomes closed under "guarded" and the stamp rides the same UPDATE. **This is
   the requirement, not a preference.**

   > **The escape hatch here was an unnumbered deferral, which this plan's own
   > rule calls a deletion (corrected 2026-07-27).** This paragraph used to offer a
   > "fallback if that is too large for PR6": stamp in place, leave both writers
   > unguarded, keep the clobber, and "record it as a known defect with a
   > follow-up." No PR number, no dependency edge, no D-number — and §8 states the
   > rule it broke: **a deferral without a number is a deletion.** An implementer
   > under time pressure takes the cheaper branch, the "follow-up" exists nowhere
   > else in this document, and a `succeeded` row silently rewritten to `failed`
   > ships as intended behaviour. Note also that this hedge does **not** match the
   > preamble's grep vocabulary — it is phrased as "fallback if", not "if so" —
   > which is the second demonstration that the grep bounds nothing on its own.
   >
   > If guarding both writers genuinely does not fit PR6, the split is **PR8**, and
   > it is a blocking dependency of PR6's exit criterion rather than a follow-up:
   > PR6 may not stamp through an unguarded writer, because the stamp and the
   > clobber ride the same UPDATE. Either the writers are guarded first or PR6 does
   > not land — there is no third branch.
   | `defer_run_terminal` (`:2028`) | **no** — writes only `result_payload_json.deferred_*`; `status` is untouched | **no** |

   **Two corrections to the revision immediately above (2026-07-27).** That
   revision named the set as "`defer_run_terminal` / `settle_deferred_run` /
   `record_run_output`, arbitrated by `_stronger_terminal_status`", and both
   halves of that were wrong.

   1. **It still missed the result-less writer** — the very path the finding was
      about. `settle_agent_runs_without_result` (`core/scheduled_tasks.py:3038`)
      calls `settle_without_result` (`:1230`), which delegates to
      `SqliteBackgroundStore.settle_run_terminal` (`storage/background.py:1949`).
      That is a fourth guarded UPDATE, and it is *not* one of the three that call
      `_stronger_terminal_status` (only `:1858` in `record_run_output`, `:2050` in
      `defer_run_terminal`, and `:2117` in `settle_deferred_run` do). So keying
      the rule on that arbitration function excluded precisely the writer the
      correction existed to include.
   2. **It included a writer that is not terminal.** `defer_run_terminal` records
      an intent while an Activity blocks the row; the status stays `running`.
      Stamping there would expose an owed failure notice for a run that has not
      failed and may yet settle successfully — a false failure notice to the
      user, which is worse than the missing one it was meant to fix.

   > The lesson, and it is the same one two rounds running. Round 30 asserted
   > "one writer owns terminal state" without enumerating the writers. Round 31
   > replaced it with a *predicate* — "calls `_stronger_terminal_status`" — and
   > again did not enumerate, so the predicate silently disagreed with the
   > property it was standing in for. Substituting a proxy for the real condition
   > is the same error as naming one call site; it just looks more principled.
   > The condition here is "this UPDATE sets `status` to a terminal value", and
   > the only way to know which writers satisfy it is to read all four. PR5's pause stays co-located with it
   (classify → maybe rebind → maybe pause → notify once); the pause predicate
   remains task-scoped, since only tasks have a definition to pause, but the
   notification must not inherit that scoping. Any run type added later gets the
   behaviour by default rather than by remembering to wire it. Reuse
   `core/backend_failure.py:emit_backend_failure`, whose `metadata.event =
   "backend_failure"` is already honored by `web_push_notifications._is_notifiable_message`.
   For a dead session, build the context from `deliver_key`/`metadata.session_scope_id`
   via `_resolve_delivery_target` + `_build_context` (`:2763`) — that is the one
   piece of new plumbing. Delivery follows **D5**'s ladder, ending in a DM whose
   body carries its own context (task name/id, creating channel/thread with a deep
   link, last success, error class, current state, how to resume). **The owner DM
   is rung (4) and does not always resolve, so the workspace-notifications
   session at §D5 rung (5) is in this PR's scope** — a caller-less CLI definition empties
   every rung above it (`vibe/cli.py:3973-3985`), so without rung (5) the notice
   has nowhere to go. The same notification serves **D1** for interrupted runs,
   with `metadata.interrupt_reason` selecting the copy.

   > **This sentence was the twin of a hedge already retired in §D5 (corrected
   > 2026-07-27).** It read "Verify the owner-DM fallback can always resolve; if
   > not, widen the workbench inbox shape." §D5's correction made that widening
   > mandatory and in-scope — but only rewrote the §7 copy, 1,700 lines away,
   > leaving the conditional standing *here*, in PR6's own step list, which is what
   > an implementer actually reads first. Two lessons, both cheap: a retired
   > phrasing has to be swept for **everywhere**, not fixed where review pointed;
   > and the inverted wording ("if not" rather than "if so") is why the preamble's
   > grep missed it. Hedges are not a fixed vocabulary.
2. **Derived health, no new state:** add `consecutive_failures` /
   `recent_failures` to `_task_payload` (`vibe/cli.py:1505`) and the harness API
   (`vibe/ui_server.py:8083`) via one indexed query over
   `agent_runs WHERE definition_id = ? AND run_type != 'watch_runtime' AND status
   IN (<verdict>) AND json_extract(metadata_json, '$.interrupt_reason') IS NULL
   AND COALESCE(completed_at, created_at) >= ? ORDER BY
   COALESCE(completed_at, created_at) DESC, id DESC LIMIT N` (the bind is
   `now - T`; batch with a window function for the list endpoint). A schema-based
   counter on `run_definitions` is the follow-up if `agent_runs` retention ever
   becomes lossy (Q6).

   **The `, id DESC` tie-break is required, not tidiness (added 2026-07-27).**
   Timestamps here are ISO strings written by the application, and several writers
   stamp a whole batch with **one** value: `write_watch_runtime` flips every prior
   heartbeat with a single `updated_at` (`storage/background.py:2503-2511`), and a
   restart terminalizes its batch with one `now_iso`. Without a secondary key,
   "the newest run" is whichever row SQLite happens to return first, so the badge
   can flip between `failing` and `healthy` across two identical reads with no
   write in between — the worst shape of bug to debug, because nothing changed.
   Master's own precedent is one line away and already does this:
   `list_pending_callbacks` orders `.order_by(completed_at, id)`
   (`storage/background.py:1521-1536`). Reuse it rather than rediscover it.

   **The time bound was in the contract and not in the query (corrected
   2026-07-27).** The health rule below defines the window as the last N runs *or*
   the last T hours, whichever is shorter, and promises it "ages out on its own and
   needs no user action". Bounded only by `LIMIT N`, this query never ages anything
   out: a definition that fails once and then stops firing — paused, one-shot, or
   simply infrequent — keeps that failure as its newest verdict forever and reads
   `failing` indefinitely. Third instance of the same shape in three rounds, so it
   gets the same treatment: the cutoff goes in the `WHERE`, before `LIMIT`, and the
   query is the intersection of both bounds rather than one of them with the other
   written in prose nearby. It reuses the same `COALESCE` expression as the
   ordering, so the expression index serves it as a range scan.

   One consequence worth naming: a task the step-4 policy **auto-paused** for
   repeated failures will read `healthy` once its last failure ages past T. That is
   consistent rather than a hole — the pause is the durable signal, it is visible on
   the row independently of health, and `last_error` stays in the payload (step 3).
   A health badge that outlived the window would be acknowledgment state by another
   route, which this step's own correction below rejects.

   **Every exclusion belongs in the `WHERE`, not in the classifier (corrected
   2026-07-27).** Two findings, one shape. The previous revision left `canceled` in
   the predicate and skipped it while classifying, and admitted interrupted rows
   entirely. Both are erased by `LIMIT N`:

   - **N cancellations bury a failure.** Cancel the retry N times after one failure
     and the cancellations fill the whole bounded window, displacing the failure —
     the definition reads `healthy` although nothing ever succeeded. Skipping rows
     *after* the limit cannot make them transparent; only the predicate can.
   - **An interruption is not a verdict about the definition.** `failed` rows
     carrying `metadata.interrupt_reason` are ordinary terminal failures to this
     query, so one deploy makes every in-flight definition the owner of a fresh
     newest failure — directly contradicting the rule below that interruptions stay
     out of this counter.

   So the window is the last N **verdicts**: `<verdict>` is `succeeded` + `failed`
   only (alias-expanded per below; `canceled` is gone from it), and interruption-class
   rows are excluded by predicate. The general rule, which is the same one round 38
   established for nonterminal rows and I then failed to apply twice: **a bounded
   window must be bounded over exactly the rows it intends to count.** `LIMIT` is
   the last thing that happens, so anything the classifier would ignore must never
   reach it.

   **The discriminator is `interrupt_reason`, not `status`, and the previous
   revision cited this wrongly.** It claimed eviction reaches health because
   "`settle_run_terminal` maps `failed` → `canceled` when `cancel_requested` is
   set". That function does do that (`storage/background.py:1949-2026`), but it is
   not PR2's path. PR2 terminalizes through `defer_run_terminal` →
   `settle_deferred_run`, which arbitrates with `_stronger_terminal_status` and
   never downgrades: `failed` outranks `canceled` in `_TERMINAL_STATUS_PRIORITY`
   (`:432-436`), so `_stronger_terminal_status("failed", "canceled")` returns
   `failed` (`:2118`, `:507-515`). An evicted run therefore settles **`failed`**,
   while the result-less restart sweep can settle the same class of event
   **`canceled`**. One event class, two statuses, decided by which writer got there
   first — which is precisely why a status test cannot express "interruption" and
   `metadata.interrupt_reason` must be the predicate. PR2 already guarantees the
   field is set on every interruption path (correction above: record the cause
   before cancelling).

   **Tolerating the disagreement is not enough — normalize it at the writers
   (corrected 2026-07-29, review).** The predicate above makes the health query
   robust to "one event class, two statuses", but the statuses themselves stay
   user-visible: a result-less interruption settled through
   `settle_run_terminal` with a stale `cancel_requested=1` records
   **`canceled`** — the UI reports user intent for an infrastructure fault
   (HFR-012/HFR-029 inverted), and any failure-keyed mechanism, the owed-notice
   stamp included, can skip the event. D1 reserves `canceled` for an explicit
   user Stop. So the rule is normative for **every** interruption writer, not
   just this query's predicate: a settlement carrying
   `metadata.interrupt_reason` settles **`failed`, unconditionally** —
   `settle_run_terminal`'s `cancel_requested` → `canceled` mapping must be
   bypassed (or out-prioritized through `_stronger_terminal_status`) whenever an
   interrupt reason is being recorded. PR2 owns the writers; this query's
   `interrupt_reason` predicate stays as defense in depth, not as the fix. Owed
   test: `test_interruption_with_stale_cancel_requested_still_settles_failed`.

   Three costs, stated rather than hidden — **the third added 2026-07-27, because
   "two costs, stated rather than hidden" implied the list was complete and it was
   not.** This is the store's **first SQL predicate
   over `metadata_json`** — master always loads the column and filters in Python
   (`storage/background.py:2248-2254`, and `list_deferred_runs` at `:1498-1519`
   fetches every queued/running row to inspect it). That idiom is fine when the
   result set is unbounded, and unusable here for the reason this correction exists:
   filtering after the fetch is filtering after `LIMIT`. SQLite's json1 is built in
   (3.49.1 in-tree), so `json_extract` needs nothing new. If the predicate ever
   measures hot, the follow-up is an expression index over
   `json_extract(metadata_json, '$.interrupt_reason')` — derived, so still no state
   anyone must keep in sync — or paging by completion order until N verdicts are
   collected, which is correct but a loop.

   **Third cost: `json_extract` aborts the whole query on one malformed row, where
   Python filtering degrades per-row.** `_json_loads` is defensive by construction
   — `try/except: return default` (`storage/background.py:30-40`) — so master's
   idiom treats this column as *possibly* invalid, and a single bad row costs one
   misclassified run. SQLite instead raises `malformed JSON` and fails the
   statement, so one bad row takes down the health badge for **every** definition
   in the list, not just its own. I could not find a currently reachable writer
   that produces invalid JSON here (every path goes through `_json_dumps`, the
   column is `NOT NULL`, and an existing migration does a bare
   `json.loads(metadata_json or "{}")` on real data without crashing —
   `storage/alembic/versions/20260723_0032_session_visibility.py:49`), so this is a
   landmine rather than a live defect: a future writer bug, a hand-edited row, or a
   torn write converts a one-row inaccuracy into a list-wide outage. Requirement:
   the health query must not be the only thing standing between a malformed row and
   an empty Harness list — wrap the read so a `malformed JSON` failure degrades to
   "health unknown" for the list rather than propagating, and keep `last_error`
   rendering independently of it. Owed test:
   `test_one_malformed_metadata_row_does_not_blank_health_for_every_definition`.

   **The canonical query must order by completion, and this step's own heading
   overclaimed (corrected 2026-07-27).** The previous revision argued for
   `completed_at` ordering in prose and left `ORDER BY created_at DESC` standing in
   the specification directly above it — an implementer reads the query. Worse, the
   prose called the mis-ordering *transient* and self-correcting, which conflated
   two different things:

   - **While an earlier-created run is still in flight**, health lags: the latest
     settled outcome is not yet the latest outcome. That one *is* transient, is
     accepted deliberately, and is the reason the settled-prefix deferral stays out
     of this query (argued below).
   - **After every run has settled**, `created_at` ordering is *permanently* wrong.
     Later-created B succeeds at t+1, earlier-created A fails at t+9; ordered by
     creation, B is newest forever, so the definition reports `degraded` when its
     most recent outcome was a failure. Nothing later fixes it. That is a defect,
     not a lag, and only completion ordering fixes it.

   `COALESCE` rather than bare `completed_at` because master does not treat
   terminal and `completed_at IS NOT NULL` as the same condition —
   `list_pending_callbacks` tests them separately (`storage/background.py:1531`,
   `:1529`) — even though all five terminal writers do stamp it today
   (`complete` → `core/scheduled_tasks.py:1608`; `settle_run_terminal` →
   `storage/background.py:1995`; `complete_coalesced_…` → `:687`, `:690`).
   Ordering must not silently reorder a row on the day one of them stops.

   The index claim changes with it: `ix_agent_runs_definition_created` is
   `(definition_id, created_at)` (`storage/models.py:260`) and does **not** serve
   this ordering, so the equality is index-backed and the sort is not. Add an
   expression index matching the `ORDER BY` exactly —
   `(definition_id, COALESCE(completed_at, created_at))`. That is DDL, so the
   heading's old "no migration" was wrong as written; what this step avoids is new
   *state* — no counter, no column anyone must keep in sync, nothing to backfill or
   reconcile. An index-only revision is established practice here:
   `20260723_0033_agent_runs_updated_index.py` adds one behind `if_not_exists`
   guards, and `ix_agent_runs_callback_status` is already
   `(callback_status, completed_at)` (`20260610_0022_agent_run_callback_session.py:35`),
   so indexing a completion timestamp is not a new shape either.

   `<verdict>` is **not** a literal `('succeeded', 'failed')`. The
   stored `status` column holds legacy spellings alongside canonical ones —
   `RUN_STATUS_ALIASES` maps `completed` → `succeeded`, `processing` → `running`,
   `pending` → `queued` (`storage/background.py:108-117`), and both vocabularies
   are live in the tree (`core/scheduled_tasks.py:97-98`,
   `storage/workbench_sessions_service.py:46`). A literal list would silently miss
   every row written as `completed`, i.e. would count successes as absent and
   report a healthy definition as failing. Expand it the way master already does:
   `_status_query_values("succeeded") + _status_query_values("failed")`, following
   `list_pending_callbacks`, which builds its terminal predicate exactly this way
   (`storage/background.py:1521-1531`) — this query simply omits the `canceled`
   term that one includes.

   **The `watch_runtime` exclusion belongs here too (corrected 2026-07-27).** The
   previous revision scanned this definition's history unfiltered, and for a
   managed watch that history contains the supervisor heartbeat row — same
   `definition_id`, `created_at` refreshed to the waiter's `started_at` on every
   restart, `status` `running` and then `succeeded`
   (`storage/background.py:2503-2525`, `:2507-2511`; full reasoning under PR6's
   streak correction). So it can present as the *newest* run for the definition
   and reset `consecutive_failures`, downgrading a genuinely failing watch to
   healthy, and it consumes slots in the bounded `LIMIT N` window that recent
   real executions need. Same constant, same predicate as the streak and
   settled-prefix queries: `_WATCH_RUNTIME_RUN_TYPE` (`storage/background.py:162`),
   following `recover_processing_runs` at `:2202`.

   **Nonterminal *executions* had to be excluded as well (corrected
   2026-07-27).** The revision immediately above removed the bookkeeping rows and
   stopped there, leaving ordinary `queued` / `running` executions of the
   definition itself in the window. Same two failure modes, from a row class the
   `watch_runtime` predicate does not touch:

   - **The newest row is not an outcome.** A failing recurring definition fires
     again on schedule; the fresh `queued`/`running` row is now the newest entry
     for `definition_id`. The health rule below reads `failing` off "the latest
     run failed", and the latest run has not failed — it has not finished — so a
     definition that is actively failing reports `degraded` or `healthy` for the
     whole duration of its next attempt. Every fire re-arms the bug, and it is
     worst precisely when turns are long, which is the normal case
     (`core/services/dispatch.py:118-121`, no turn-duration timeout by design).
   - **Backed-up executions eat the window.** `LIMIT N` is applied after the
     predicate, so overlapping or queued-up rows displace completed failures out
     of a bounded window and `consecutive_failures` undercounts. `create_per_run`
     definitions hold no execution lock (`core/scheduled_tasks.py:2609-2611`,
     `:2622-2623`), so overlap is by design, not an anomaly.

   This is the third row class this one query has had to learn to exclude, so
   state the rule rather than the patch: **`agent_runs` filtered to a definition
   is a history of rows, and health is a function of settled outcomes only.** A
   row earns a place in the health window by having finished, not by existing.

   The **settled-prefix predicate stays out of this query**, and that asymmetry is
   deliberate rather than an oversight. Overlap means completion order need not
   follow `created_at` (same fact, argued in full under PR6's streak correction),
   so a health read can transiently show the wrong newest outcome while an
   earlier-created run is still in flight. PR6's streak defers classification
   until the prefix settles, because a duplicate or wrong-streak *notice* is
   unrecoverable. Health is a display value recomputed on every read, so once the
   straggler settles the completion ordering specified above puts its outcome
   newest with no further action; blocking a UI field behind an hours-long turn
   would trade a briefly stale badge for a permanently empty one. **Do not copy
   the deferral machinery here** — that is the mirror-image error to
   under-filtering. Note the division of labour precisely, because the previous
   revision blurred it: completion ordering fixes the *permanent* mis-ordering
   after settlement, and only the transient in-flight lag is left tolerated. This
   paragraph licenses the lag, not the mis-ordering.

   Step 3 inherits this rather than needing its own fix: `_task_last_status`
   reads the task definition's own `last_run_at` / `last_error` fields today
   (`vibe/cli.py:1510-1515`), not `agent_runs`, so it is not independently
   exposed — but once PR6 makes it consult `recent_failures`, it consults a query
   that already filters. Noted explicitly so the predicate is not
   over-applied to a function that never reads run rows.
3. **Fix the reporting bug:** `_task_last_status` must not report `succeeded`
   while recent failures exist; include `last_error` in the `brief` payload; add
   a health badge to Harness list rows, not just the detail pane.

   **"Unacknowledged" was not implementable as written (corrected 2026-07-27).**
   The requirement previously said "while *unacknowledged* failures exist", but
   the plan defines no persisted acknowledgment state and no user action that
   records one, and neither `consecutive_failures` nor `recent_failures` can
   express it. An implementer had two choices, both wrong: treat any historical
   failure as permanently unacknowledged, so a task warns forever after its first
   bad run; or let the next success clear it, which is P6 — the exact bug this PR
   exists to fix — reintroduced.

   Resolved by making the rule precise and derivable instead of introducing
   acknowledgment state: health is **`failing`** when `consecutive_failures >= 1`
   (the latest run failed), **`degraded`** when the latest run succeeded but any
   failure falls within the health window, and **`healthy`** otherwise. The
   window is count-and-time bounded — the last N runs *or* the last T hours,
   whichever is shorter — so it ages out on its own and needs no user action, and
   a single success downgrades `failing` to `degraded` rather than erasing it.
   Both values come from the query already specified in step 2, so this adds no
   new state.

   **`canceled` is transparent, and saying so is required (corrected
   2026-07-27).** The rule above named only "the latest run failed" and "the latest
   run succeeded", while step 2's window admits `canceled` as a terminal status.
   A canceled newest row matched neither clause and fell through to **`healthy`** —
   so cancelling an attempt after a failure would *clear* the failing badge, by hand
   (fail, then cancel the retry) and eventually by machine. P6 by a third route,
   after this document has twice closed the other two.

   The rule: **a cancellation is the absence of an outcome, not an outcome.** It
   neither counts as a failure nor closes failure history. `failing` and `degraded`
   are judged on the newest row that actually ran to a verdict, and a window
   containing nothing but cancellations is `healthy` because nothing has been
   observed to fail.

   **Where that rule is enforced was itself wrong (corrected 2026-07-27).** The
   revision above put the skip in the classifier, which does not work: `canceled`
   rows still counted against `LIMIT N`, so N cancellations displace the failure
   they were supposed to be transparent to. The exclusion now lives in step 2's
   predicate, and the same applies to interruption-class rows — full argument and
   the corrected `settle_deferred_run` citation are there. This clause is
   consequently a *statement about* step 2's window, not a second filter applied on
   top of it; implementing it twice is how the two drift apart.

   This is chosen for consistency, not convenience: PR6's streak already closes
   only on `succeeded`, so `canceled` is already transparent there. Making health
   agree means one predicate — *only success clears* — instead of two rules that
   drift, which is how the last several defects in this section were produced.

   Two consequences, stated so neither is mistaken for an oversight. Repeated
   interruption does **not** raise `consecutive_failures`, so an eviction loop
   never trips the step-4 auto-pause; that is correct, because the definition is
   not what is broken and step 4 restricts auto-pause to the unresolvable-target
   class anyway — interruptions surface through **D1** notices, which name their
   own `interrupt_reason`. And if operators later want interruptions visible on the
   row, that is a separate `interrupted` indicator alongside health, not a fourth
   value smuggled into it.

   This is deliberately weaker than acknowledgment: it answers "has this task
   been unhealthy recently" rather than "has a human seen it". If maintainers
   want true acknowledgment — a badge that stays until dismissed — that needs
   persisted state plus a dismiss action, and should be a separate item rather
   than smuggled in through a derived counter. Flagging the choice rather than
   assuming it.
4. Policy: notify on the 1st failure (once, not daily); auto-pause at 3
   consecutive failures **only** for the unresolvable-target class — a transient
   agent error must not disable a task.

   **The class needs a persisted code, or auto-pause fires on the wrong thing
   (corrected 2026-07-29, review).** "Unresolvable-target class" is not
   implementable from what is persisted today: `_execute_task` collapses every
   exception to `str(exc)`, and `resolve_session_id_target` wraps a transient
   SQLAlchemy failure in the same `ValueError` shape as a genuinely missing
   session — so three temporary database errors would classify as an
   unresolvable target and pause a valid recurring definition. PR6 therefore
   persists a **stable failure code stamped at the resolver boundary** (e.g.
   `metadata.failure_code = "unresolvable_target"`, written only where the
   resolver *knows* the target is missing; transient infrastructure errors map
   to a distinct code), and the auto-pause counter keys **only on that code** —
   never on exception type or message text. Owed test:
   `test_transient_resolver_errors_do_not_auto_pause_a_definition`.
5. **The owed-failure-notice drain (assigned 2026-07-27).** Not just the
   copy — the whole delivery mechanism: scan `metadata.owed_failure_notice ==
   "pending"` on the existing 2 s tick, deliver through (1) above, and acknowledge
   under the protocol specified in PR7's restart correction (structured
   `(delivered_id, persisted_row, error)` result, ack on either evidence of
   delivery, `attempts`/`next_attempt_at` backoff, `failed` dead letter, and the
   `persist_agent_message` error channel). **This must land with PR6 and not with
   PR7**, because PR2 depends on PR6 and starts writing `pending` notices — see
   the §5 ownership correction. It is the larger half of PR6 by implementation
   weight, so size the review accordingly; if PR6 has to be split, the drain is the
   half PR2 blocks on.

   **The drain must cover every first failure transition, not only interruptions
   (corrected 2026-07-27).** As originally scoped it keyed on
   `metadata.owed_interruption_notice`, which left ordinary P6 failures —
   unresolvable target, backend error, anything without an `interrupt_reason` —
   on the direct `emit_backend_failure` path with no durable record. That path
   re-raises on failure (`core/backend_failure.py:146-148`), so if the very first
   failure transition coincides with an IM send or persistence failure, nothing
   is stored, nothing is retried, and step 4's "notify on the 1st failure (once,
   not daily)" then *suppresses* notification on every later consecutive failure.
   The task is silently broken forever, which is P6 exactly — reintroduced by the
   policy written to fix it, in the sub-step next to it.

   So the owed-notice state is not interruption-specific. It is renamed
   `owed_failure_notice` throughout this document — every producer, scanner and
   protocol reference, not just this sub-step — with `interrupt_reason` demoted
   to an optional field selecting copy. A split key would have been worse than
   either name alone: the drain would find only half the notices, and which half
   depended on which section the implementer read. Stamp it on **every** first
   failure transition before attempting
   delivery, so the retry/dead-letter protocol above covers the ordinary case
   too. The suppression policy must key on *acknowledged* notices rather than on
   "we called notify once" — an attempt that raised is not a notification.

   **A pending callback is already a delivery for the same transition, and the
   two drains must not both fire (corrected 2026-07-29, review).** For a failed
   run carrying `callback_session_id`, terminalization leaves
   `callback_status='pending'` and `_drain_callbacks` delivers a user-visible
   result — the path this plan elsewhere treats as sufficient notification.
   Stamping `owed_failure_notice` unconditionally on that row would give one
   failure two independently keyed messages. The stamp stays unconditional
   (durability: the callback can still fail), but the **notice drain defers
   while a callback delivery for the same run is `pending`**, and resolves on
   its outcome: callback `sent` → the owed notice is acknowledged as
   delivered-by-callback (`skipped` — it is a duplicate); callback `failed` or
   dead-lettered → the notice becomes deliverable and the retry protocol
   proceeds. One transition, one message, and the fallback still exists the
   moment the primary path dies. Owed tests:
   `test_failed_run_with_callback_delivers_exactly_one_message`,
   `test_owed_notice_takes_over_when_the_callback_dead_letters`.

   **Suppression needs a scope, or "once, not daily" is not implemented
   (corrected 2026-07-27).** Keying on acknowledged notices says *when* a notice
   stops being owed; it does not say *which* notices are the same notice. Each
   execution of a recurring definition is a new run, so "stamp on every first
   failure transition" plus a run-derived `failure_id` makes every consecutive
   failure a distinct, unacknowledged notice — and the drain notifies on every
   fire. That is precisely the daily-spam behaviour step 4 forbids, produced by
   the correction written two paragraphs above it.

   The missing scope is the definition's **current consecutive-failure streak**:
   for `definition_id = D`, the maximal run of terminal executions ordered by
   `created_at` and ending at this run with no `succeeded` in between. A
   `succeeded` run closes the streak, so the next failure after a recovery
   notifies again — which is the behaviour "notify on the 1st failure" is
   actually describing. The lookup is cheap and already indexed:
   `agent_runs.definition_id` exists (`storage/models.py:221`) with
   `ix_agent_runs_definition_created` on `(definition_id, created_at)` (`:260`).
   **A run with `definition_id IS NULL`** — the column is nullable, and one-off
   or ad-hoc runs have none — **has no streak and is therefore never suppressed**;
   every such failure is a first failure.

   **Interruptions are a separate lane, or D1 notices get suppressed as duplicates
   (found while fixing step 2's query, 2026-07-27).** Not reported by review; it
   follows from the same fact. An interrupted run settles `failed`
   (`_stronger_terminal_status` keeps `failed` over `canceled`,
   `storage/background.py:2118`, `:432-436`), so on the definition above it joins
   the ordinary failure streak. Step 1 routes **both** kinds of notice through one
   mechanism — "the same notification serves D1 for interrupted runs, with
   `metadata.interrupt_reason` selecting the copy". Compose the two and the outcome
   is silence: a definition already failing has a canonical notice for its streak,
   the eviction's row joins that streak, its notice is `skipped` as a duplicate, and
   the user is never told a deploy killed their run — which **D1** requires
   unconditionally. Worse, it is the sick definitions that lose the message, since a
   healthy one has no streak to be absorbed into.

   So streak membership is **verdicts only**: interruption-class rows neither join a
   streak nor close one, exactly as in step 2's window, and for the same reason —
   they are not evidence about the definition.

   **The second suppression scope was the same bug one lane over (corrected
   2026-07-27).** The fix above went on to give interruption notices "their own
   suppression scope, keyed the same way (consecutive interruptions of `D` sharing
   one `interrupt_reason`)". That was reasoning by analogy from the failure lane,
   and the analogy does not hold — it reintroduces exactly the silence it was
   written to fix, one restart later. A single restart interrupts **every**
   in-flight execution of `D` at once, and `create_per_run` means there can be
   several: `_execution_lock_key` returns `None` for that policy
   (`core/scheduled_tasks.py:2609-2611`, `:2622-2623`), so executions genuinely
   overlap. Those runs are consecutive interruptions of `D` sharing one
   `interrupt_reason` — the key's own definition — so one notice is derived from
   one arbitrary run and the rest are `skipped`. Each skipped run is a distinct
   turn, with its own session, prompt, and rerun path, and its user is told
   nothing about it.

   **Interruption notices are therefore per-run, always, with no suppression
   scope.** The bounding argument that justifies suppression for failures does not
   exist here, and that asymmetry is the reason rather than an exception:

   - A recurring definition can fail *unboundedly* — every tick produces another
     failure, so without a streak the user gets a message per tick forever. That is
     what the failure scope is for.
   - A run can be interrupted **at most once**. D1 terminalizes it and it is never
     re-dispatched, so the notice count is bounded by the number of interrupted
     runs, which is bounded by the number of runs. Per-run notices are
     self-bounding; there is nothing to suppress.
   - A restart loop therefore does not produce N notices *for one run* — it
     produces one notice each for N different runs, which is the correct count.

   If one restart interrupting several runs should read as one message, that is
   **coalescing at delivery** — one notice enumerating the affected runs — and
   explicitly **not** a `skip`. The distinction is the one D1 turns on: a coalesced
   notice still accounts for every run, while a skipped notice drops runs on the
   floor. Owed test:
   `test_one_restart_interrupting_overlapping_runs_notifies_for_every_run`,
   asserting the count equals the number of interrupted runs — not merely that a
   notice was sent, which is the assertion this correction would have passed.

   > This is the fourth row class this section has had to separate — bookkeeping
   > rows, nonterminal rows, cancellations, now interruptions — and the second time
   > the fix had to be applied in two places at once. The recurring error is not any
   > one predicate. It is treating `agent_runs` as a log of comparable events when it
   > is a log of rows whose meaning depends on `run_type`, `status`, *and* metadata.

   **The streak is only computable over a settled prefix (corrected
   2026-07-27).** "Terminal runs ordered by `created_at`" reads as if the terminal
   subset were the finished part of the history. It is not, because executions of
   one definition can overlap: `_execution_lock_key` returns `None` for
   `session_policy == "create_per_run"` by design — a fresh session each time
   needs no serialization (`core/scheduled_tasks.py:2609-2611`, `:2622-2623`).
   So completion order need not follow `created_at`. Later-created run B fails,
   is the earliest *terminal* failure, becomes canonical and sends; earlier-created
   run A then fails, becomes the new earliest, and the streak emits a second
   notice for the same outage. The canonical choice was never stable, so
   round 34's "at most one notice per streak" did not hold either.

   Therefore **classification is deferred while any earlier-created run of the
   same definition is nonterminal.** A row is classified only once every run of D
   with a lower `(created_at, run_id)` has settled; until then its notice stays
   `pending` and undelivered, and the next drain tick reconsiders it. That makes
   the streak a function of a settled prefix, which cannot be rewritten by a
   straggler.

   Two notes on the cost, because deferring on a nonterminal run is exactly the
   shape this plan is otherwise suspicious of. First, it is free for every
   definition that *does* hold an execution lock — those executions serialize, so
   the prefix is always settled and the predicate never fires; only
   `create_per_run` pays. Second, the wait can be long, since an earlier turn may
   legitimately run for hours (`core/services/dispatch.py:118-121`, the
   no-turn-duration-timeout invariant), so a failure notice can be delayed behind
   a straggler. That is accepted deliberately: a delayed notice is recoverable and
   a duplicate or wrong-streak notice is not, and the notice is not time-critical.
   It also still satisfies **a deferral without a number is a deletion** — the
   bound is the earlier run's settlement, and settling every nonterminal run is
   precisely what PR1/PR2/PR7 guarantee. If those guarantees fail, the deferred
   notice is not the defect worth worrying about.

   **Both queries must range over *executions*, and `agent_runs` also holds
   bookkeeping rows (corrected 2026-07-27).** For every active managed watch,
   `write_watch_runtime` stores a row with `id = f"runtime:{watch_id}"`,
   `request_type="watch_runtime"`, `definition_id = watch_id`, `created_at` = the
   waiter's `started_at`, and `status="running"` for as long as the waiter lives
   (`storage/background.py:2503-2525`). A watch's actual hook executions carry
   `run_type="watch"` with `definition_id = watch.id` — **the same
   `definition_id`** (`core/watches.py:1320-1321`). The heartbeat is intentionally
   nonterminal, and master already excludes it from recovery
   (`recover_processing_runs`, `:2202`) behind a named constant whose comment
   describes this exact hazard: counting the heartbeat as an execution "would make
   every healthy waiter read as running" (`_WATCH_RUNTIME_RUN_TYPE`, `:157-162`).

   Unfixed, this breaks the design in **two** directions, and the second is worse
   than the reported one:

   1. **Deferral never releases.** The heartbeat is earlier-created and
      permanently nonterminal, so every failed watch run defers behind its own
      supervisor forever and a long-running watch never delivers a failure
      notice. That is P6 for watches, reintroduced by the mechanism written to
      guarantee notification.
   2. **The streak is silently broken open.** Each heartbeat write first flips
      the previous `watch_runtime` rows to `succeeded` (`:2507-2511`). A
      `succeeded` row bearing the watch's `definition_id` sits between any two
      failures, so it *closes the streak* — every watch failure reads as a first
      failure and notifies. Fixing only the deferral predicate would trade a
      permanent silence for exactly the daily spam this whole sub-step exists to
      prevent.

   So **both** the settled-prefix predicate and the streak-membership query
   exclude `_WATCH_RUNTIME_RUN_TYPE`, reusing master's constant and following the
   idiom already established at `:2202` rather than inventing a second spelling.
   The general rule, stated because the specific fix will not generalize on its
   own: `agent_runs` is not a table of executions, it is a table of rows *some* of
   which are executions, and any predicate over "this definition's history" must
   say which.

   > Third time in seven rounds that a rule was written over a state space I had
   > not enumerated — the terminal writers, then the terminal-implies-settled
   > premise, now the row space itself. The failure is not the individual wrong
   > answer; it is reaching for a plausible property instead of listing what is
   > actually in the set.

   Owed: `test_out_of_order_completion_does_not_resend_for_one_streak`,
   `test_classification_defers_while_an_earlier_run_is_still_running`,
   `test_watch_runtime_heartbeat_does_not_defer_a_failed_watch_run`, and
   `test_watch_runtime_heartbeat_does_not_close_a_failure_streak`.

   **"No acknowledged notice yet" is not permission to send (corrected
   2026-07-27).** The predicate above suppresses a later notice only once an
   earlier one is acknowledged, which leaves the window that matters wide open: if
   the streak's first notice fails its transport attempt and a second execution
   fails before that retry lands, nothing in the streak is acknowledged, so the
   second row passes the predicate and notifies — while the first row is still
   `pending` **by design**, because the correction two paragraphs above
   deliberately keeps it retrying. One streak, several notices, produced by the
   interaction of the two fixes rather than by either alone. Absence of an
   acknowledgement is not evidence that nothing is in flight.

   So the streak has **one canonical notice** — the earliest `pending`-or-later
   notice in the streak by `(created_at, run_id)`, the run id only as a
   deterministic tie-break — and every other row in the streak is gated on its
   outcome:

   | canonical notice | later rows in the same streak |
   |---|---|
   | `pending` | **deferred** — not delivered, no attempt consumed, no backoff burned |
   | `sent` | `skipped` (the streak's notice was delivered; these are duplicates) |
   | `failed` (dead-lettered) | the earliest remaining `pending` row is **promoted** to canonical and may send |

   Promotion on dead-letter is the part that keeps this honest: a streak whose
   canonical notice exhausted its retries still owes the user the news that the
   task is broken, so the streak's claim on delivery outlives any single row. And
   at most one notice per streak is ever in flight, which is the property "once,
   not daily" was asking for.

   This deferral satisfies the plan's own rule that **a deferral without a number
   is a deletion**: the bound is not a timer but the canonical notice's own
   resolution, and that is already bounded — every notice reaches `sent` or
   `failed` under the existing `attempts`/`next_attempt_at` protocol. A deferred
   row cannot wait forever unless a canonical notice can retry forever, which the
   dead-letter bound forbids.

   **Suppression is applied by the drain, not by the terminal writers**, and this
   is a deliberate departure from the placement the finding proposed. Three
   reasons. (a) The invariant this section spent four review rounds establishing
   is that *every* terminal transition stamps, unconditionally — five writers now
   share it, and a policy predicate inside each of them is five chances for them
   to disagree, which is the exact failure mode already paid for. (b) A streak
   read at stamp time races with concurrent executions of the same definition; at
   drain time one component reads it once. (c) Definition-level notification
   policy does not belong in `storage/background.py` UPDATE helpers. The
   requirement the finding actually states is still met: the streak's original
   `pending` notice keeps retrying, because it is not suppressed by itself.

   A suppressed notice resolves to **`skipped`** — the state the notice vocabulary
   already defines as "a row the renderer decides needs no user-visible notice"
   (state machine below). No new state is introduced: streak suppression *is*
   that decision, and adding a sixth term for it would fork a vocabulary that
   deliberately mirrors `callback_status`. `skipped` is terminal, never delivered,
   never retried, and explicitly not `sent`, since `sent` means evidence of
   delivery and this one was never attempted. Keeping the row rather than deleting
   it is the same delivery-evidence discipline as everywhere else in this plan,
   and it gives `vibe task show` the true count of failures in the streak rather
   than the count of notices that happened to be delivered.

   Owed: `test_first_failure_with_failing_transport_still_notifies_on_retry`,
   `test_suppression_does_not_apply_to_an_unacknowledged_first_notice`,
   `test_second_failure_in_streak_is_skipped_not_notified`,
   `test_second_failure_defers_while_first_notice_is_still_pending`,
   `test_dead_lettered_canonical_notice_promotes_the_next_pending_row`,
   `test_failure_after_a_success_notifies_again`, and
   `test_run_without_definition_id_is_never_suppressed`.

### PR7 — P1: settle scheduled/watch runs at the real terminal result

The end state the docs already claim as deferred. Changes:

- `TaskExecutionResult` gains `complete_on_return` (`:211-215`); honored at
  `:2446`. **The signal must be honored on the direct-request branch too
  (corrected 2026-07-29, review):** only the `task_run`/`scheduled` branch of
  `_execute_claimed_request` consults a result object — `watch`, `hook_send`,
  and `webhook` invoke `_execute_request` directly with `should_complete` left
  `True`, so a Workbench-targeted watch returns at gate submission and the
  outer `finally` still settles the row at dispatch, where the guarded
  outbound recorder can no longer correct it. PR7 defines one
  out-of-band-completion signal and honors it on **every** branch that can
  reach the gate lane — at minimum `watch`, or half of the stated migration
  (all 67 live watch rows are in P1's evidence) does not happen. Owed test:
  `test_watch_avibe_run_settles_at_terminal_result_not_at_gate_submit`.
- `_execute_task`/`_execute_request` route through
  `dispatch_turn(..., on_chunk=noop)` mirroring `:2598-2609` — **but this covers
  only the IM lane; see the gate correction below.**
- `mark_task_result` (`:2516`) moves to terminal time — **otherwise
  `vibe task list` keeps reporting `succeeded` at dispatch even after the run row
  is honest.**

No schema change, no new status value; only new rows get honest timing.

**Avibe-targeted runs never reach that dispatch, and the noop sink does not
settle them (added 2026-07-27).** `_execute_request` short-circuits before
`handle_scheduled_message`: for `target.platform == "avibe"` with a session id and
a live gate it does `await gate.submit_scheduled(...)` and `return None`
(`core/scheduled_tasks.py:3258-3261`). The surrounding comment says why that
routing exists — the gate is what makes a scheduled turn queue behind an active
Chat turn instead of preempting it, and what gives it the `in_flight` +
`turn.start`/`turn.end` lifecycle the Chat page renders (`:3248-3257`). So
bypassing the gate to reach `dispatch_turn` is not an option: it would regress
per-session serialization and the turn lifecycle, trading one bug for a worse one.
And `submit_scheduled` returns while the turn is still running, so a noop sink
placed on the path it never takes settles nothing — Workbench runs stay
prematurely settled exactly as today.

The seam already exists, but it is **not** `settle_agent_runs_without_result`.

> **Retraction (2026-07-27, same day) — do NOT extend the turn-lane helper to
> positive settlement.** Round 13 prescribed exactly that. It cannot work and it
> would be actively harmful. `SessionTurnManager` receives a
> `TurnDispatchOutcome`, which carries only `error` and `settled_by`
> (`core/services/dispatch.py:71-72`) — no result text, no message id, no output
> semantics — so the turn-lane helper has nothing to attach and the "with the
> result attached" clause was unimplementable as written. Worse, adding a second
> component that writes terminal status would make it a competing terminal writer
> racing the one that already exists.

The component that already does this is the **outbound result path**.
`_record_agent_run_terminal_result` (`core/message_dispatcher.py:1125`) runs right
before `_signal_turn_complete` on every terminal-result path (`:1480`/`:1496`,
`:1555`/`:1566`, `:1922`, `:1978`/`:2002`) and holds precisely what settlement
needs: the text, the `is_error` status, the `message_id`, and the `MessageOutput`
semantics that decide `settles_run` and
`requires_delivery_for_run_settlement` (`:1137-1155`). This is not a new idea
being bolted on — `dispatch.py:64-68` already states the contract: only
`SETTLED_BY_TERMINAL_RESULT` means "the out-of-band writer will settle the
record", and this recorder *is* that writer.

It is scoped too narrowly today, and widening it is **already PR1's job**: at
`:1149` the fallback to `_coalesced_task_execution_ids` is gated on
`payload.get("task_trigger_kind") == "agent_run"`, so a `scheduled` or `watch`
trigger collects no run ids and returns at `:1152` without recording anything.
Once PR1 widens that gate, the gate lane settles positively through the same
recorder as every other terminal result, on both lanes, with no new writer. So:

- **positive settlement**: the outbound recorder, widened by PR1 to
  scheduled/watch trigger kinds. It already routes through the shared guarded
  writers, so `_stronger_terminal_status` keeps arbitrating.

  **"Sole writer" below means sole writer of a *terminal result*, not of a
  terminal status (clarified 2026-07-27).** A gated turn can end with no terminal
  result at all — an OpenCode failure path that emits only a `notify` and then
  calls `mark_turn_complete` — and that row is settled by
  `_settle_turn_owned_agent_runs` (`core/session_turns.py:797`, called at
  `:1128`) through `settle_agent_runs_without_result`
  (`core/scheduled_tasks.py:3038`). PR7 must state explicitly whether it
  suppresses that path too or leaves it standing; suppressing it without a
  replacement recreates the zombie for result-less turns, and leaving it
  unmentioned is how PR6's notification came to miss the same path (§ PR6
  notification correction). Both writers reach the same guarded UPDATE, so
  arbitration is unaffected — what is affected is any statement of the form "one
  writer owns this".

  **But it may not stay best-effort once it is the sole writer (corrected
  2026-07-27).** The previous revision said "unchanged in shape", which is wrong
  in the one way that matters. Today the recorder's `except Exception` only
  re-raises when `require_confirmation` — i.e.
  `semantics.requires_delivery_for_run_settlement` — is set
  (`core/message_dispatcher.py:1169-1172`); for an ordinary scheduled/watch
  terminal write that flag is false, so a transient SQLite error is logged and
  swallowed and `_signal_turn_complete` releases the waiter anyway. That is
  survivable *today* only because the scheduler's own completion writer settles
  the row afterwards. PR7 suppresses that writer, and the turn-lane helper
  deliberately ignores `SETTLED_BY_TERMINAL_RESULT` — so after PR7 a swallowed
  write leaves the run **permanently `running` with no writer left to retry it**:
  the exact zombie class this whole plan exists to remove, newly created by the PR
  meant to fix it, and reachable from a single transient DB error.

  PR7 must therefore close that hole as part of making the recorder authoritative.
  What is **not** acceptable is the current combination: swallow the error,
  release the waiter, and remove every other writer.

  **The prescription is a durable retry, and "just propagate the exception" is
  ruled out (narrowed 2026-07-27).** The previous revision offered propagation as
  the smaller of two acceptable options. It is not acceptable in that form, and
  the reason is an ordering hazard, not a preference. **Every** recorder call site
  precedes the turn release: `:1922` and `:1978` both run before `_stream_chunk`
  with `completes_turn` (`:1989-1996`) and before `_signal_turn_complete`
  (`:2002-2005`), and the `finally` at `:2008` only finishes the processing
  indicator — it does **not** release the turn. So an exception out of the
  recorder skips the release entirely and `dispatch_turn_with_outcome` blocks at
  `done.wait()` (`core/services/dispatch.py:174`) **forever**, because by design
  there is no turn-duration timeout to rescue it (`:118-121`). That is strictly
  worse than the bug being fixed: not a stranded row that a sweep can find, but a
  hung coroutine holding its session, with the drain-lane fallback never reached
  and the row still `running`. The comment at `:1998-2001` says this out loud —
  that release exists precisely so waiters do not wait forever — and propagating
  past it re-creates the condition it was written to prevent.

  So: **keep the swallow, and release the turn normally** — that half stands. What
  does *not* stand is the recovery mechanism the previous revision named.

  **An in-band retry enqueue cannot be the durable backstop (corrected
  2026-07-27).** The previous revision said the recorder "enqueues a retry the
  drain owns", reusing PR6's bounded backoff and dead letter. That is circular.
  PR6's machinery scans retry state **already persisted in `agent_runs`
  metadata** — but this path is reached precisely *because* a SQLite write just
  failed, so the same outage that lost the terminal payload very plausibly loses
  the retry marker too. And even with a healthy DB there is a crash window: the
  waiter is released, the process exits before the enqueue commits, and neither
  the payload nor any marker survives. Both cases end with the row `running`
  forever, which is the outcome this whole subsection exists to prevent. A retry
  record is only durable if the write that records it is itself reliable, and here
  it demonstrably is not.

  The backstop has to be something that requires **no write at failure time**, and
  the right shape is a **sweep over rows left `running` with no live execution**.
  `recover_processing` (`core/scheduled_tasks.py:886` →
  `storage/background.py:2195 recover_processing_runs`) is the existing precedent
  for "settle whatever a crash left behind". It derives the need to act from the
  row's own state rather than from a marker somebody had to successfully write,
  which is exactly the property the retry marker lacks.

  **But that sweep does not cover this case today, and saying "one already exists"
  was wrong (corrected 2026-07-27).** Two separate gaps, both verified:

  1. **Wrong run types.** The orphaned-`running` branch is restricted to
     `run_type == "agent_run"` (`storage/background.py:2425`), and the comment
     there says why in as many words: *"Restricted to `agent_run` on purpose: when
     `scheduled`/`watch` rows settle is owned by a separate plan. Widen only
     alongside it."* This is that plan. The widening is ours to do, not something
     to inherit.
  2. **Wrong cadence.** `recover_processing` is called once, from service init
     (`core/scheduled_tasks.py:1713`). If the terminal write and the retry enqueue
     both fail while the service stays up, nothing sweeps until the next restart —
     so the run sits `running` for as long as the process lives, and
     `test_retry_enqueue_failure_still_leaves_the_run_recoverable` could only pass
     by staging an artificial restart, which would be the test lying about the
     property it claims to check. The periodic reconcile of this shape is **PR2
     step 3**.

  So the guarantee has a dependency the graph did not record: **PR7 requires PR2**,
  not only PR1 and PR6. PR7 additionally owns widening the `:2425` predicate to the
  harness run types, alongside the settlement change that makes those rows
  sweepable in the first place — the two must land together, because widening the
  sweep before scheduled/watch rows settle honestly would age out rows that are
  legitimately mid-turn.

  **Widening that predicate needs an exemption first, or it fails legitimate
  followers (added 2026-07-27).** When a scheduled/watch run targets an
  already-busy avibe session, `submit_scheduled` persists a queued segment and
  returns `"enqueued"` — but the scheduled/watch call site **discards the route**
  (`core/scheduled_tasks.py:3260-3261` awaits it and returns `None`), unlike the
  `agent_run` path which consumes it and requeues (`:2976-2978`). So the run sits
  `running` in the scheduler's view while its actual work is a durable queue row
  the gate will flush when the predecessor finishes. The live turn owns only the
  *predecessor's* run ids, so `owned_agent_run_ids` does not cover it (`:2412`),
  and the existing `busy_session_ids` exemption is wired **only into the `queued`
  hold class** (`storage/background.py:2448`) — the `running` branch at
  `:2422-2428` has no equivalent. Widen `:2425` without addressing that and the
  sweep will eventually fail a follower that was always going to run.

  This hazard is not hypothetical: `:2359-2364` documents exactly it for the
  sibling case, down to the reasoning ("a run the gate parked behind a live turn is
  NOT reported by `owned_agent_run_ids` … so a legitimate Workbench turn outliving
  `hold_ttl_seconds` would have its own queued follower failed"). The `running`
  branch simply never needed the same protection while it excluded harness runs.
  PR7 is what removes that exclusion, so PR7 owes the exemption.

  Two acceptable resolutions, and unlike the earlier two-option passage these
  genuinely differ in kind rather than in safety:

  - **Exempt it** (this is the one to take): extend `busy_session_ids` to the
    widened `running` branch, or mark the gate-queued provenance on the row and
    skip those in the sweep. It leaves the row briefly overstating its state, but
    it introduces no new durable state that recovery has to learn about.
  - **Consume the route**: make `:3260` honor `"enqueued"` the way `:2976-2978`
    does — requeue the run and let it be claimed when the gate flushes it. Larger
    than it looks; see the correction immediately below before choosing it.

  > **Correction (2026-07-27, same day) — "consume the route" was listed as
  > preferred and is not safe on its own; the asymmetry it "fixes" is
  > load-bearing.** Requeueing leaves the run `queued` with
  > `workbench_queue_holds_run` plus its durable message row, and across a service
  > restart **no component reclaims it**: `list_pending()` explicitly excludes held
  > rows (`core/scheduled_tasks.py:1085`), `recover_processing_runs()` only handles
  > `running` rows, and `SessionTurnManager.recover_persisted_agent_run_queue`
  > accepts only `task_trigger_kind == "agent_run"` (`core/session_turns.py:1490`)
  > and `run_type == "agent_run"` (`:1508`). The follower is stranded — a *worse*
  > failure than the sweep aging it out, because nothing surfaces it at all.
  >
  > Which explains the asymmetry I proposed removing: `:2976-2978` consumes the
  > route **because** `agent_run` is exactly the run type persisted-gate recovery
  > covers. The scheduled/watch site does not consume it because that recovery does
  > not extend there. Making the two paths symmetric requires widening
  > `recover_persisted_agent_run_queue` to the harness trigger kinds and run types
  > **first** — a third recovery surface for PR7 to own, on top of the periodic
  > reconcile and the widened orphan predicate. That may still be the better end
  > state, since it removes the lying row rather than exempting it, but it is not
  > the smaller option and must not be taken as a drive-by.
  >
  > Pattern worth naming, since this is the third round it has appeared: I keep
  > preferring the option that reads as cleaner without tracing what recovers it
  > after a restart. Any resolution here must answer "what reclaims this row if the
  > process dies right now?" before it is called preferred.

  **Neither resolution survives restart without a durable marker, and the reason
  is that D1's rule has a false premise here (added 2026-07-27).** Applying the
  question above to the exemption itself: it does not survive either. A parked
  follower's row is `running`, and on restart `ScheduledTaskService.__init__`
  calls `recover_processing()` synchronously (`core/scheduled_tasks.py:1713`),
  which under D1 terminalizes **every** `running` scheduled/watch row with
  `interrupt_reason=restarted`. `busy_session_ids` cannot help: it guards only the
  periodic orphan sweep, and at startup it is empty anyway because no turns are
  live yet. `recover_persisted_agent_run_queue` runs later still, from the
  internal-server startup path (`core/internal_server.py:724-727`), by which time
  the row is already `failed`. So the legitimate follower is failed rather than
  resumed — the same stranding as the other option, reached through the recovery
  path instead of around it.

  The deeper problem is that **D1's premise does not hold for this row.** D1
  terminalizes on restart to avoid a duplicate prompt: a mid-flight daily report
  re-sent after restart posts twice. But a gate-parked follower **never
  dispatched** — its prompt was never delivered to a backend, and its durable
  queue row is the thing that would deliver it. Terminalizing it prevents nothing
  and destroys work that was always going to run. D1 keys off `running` as a proxy
  for "was mid-flight", and the gate handoff is exactly where that proxy breaks,
  because `running` there means "accepted, not yet started".

  So the fix is shared by both resolutions and must land with either: the
  gate-parked state has to be recoverable from durable state rather than from
  live in-memory state that a restart erases, and `recover_processing_runs` must
  **skip rows in that state** rather than terminalizing them, leaving them for
  `recover_persisted_agent_run_queue` to resume — which, per the round-20 finding
  above, means that recovery must **first** be widened past
  `task_trigger_kind == "agent_run"` (`core/session_turns.py:1490`) and
  `run_type == "agent_run"` (`:1508`), or skipping the row only converts a wrong
  failure into a silent leak. Note the phase gap that makes this easy to get
  wrong: the two routines run in different components at different startup
  phases. So the widening that round 20 identified as the cost of "consume the
  route" turns out to be unavoidable for the exemption as well; it is a PR7
  prerequisite either way, and the choice between the two options is now only
  about where the parked row sits, not about whether the third recovery surface
  gets built. PR7 owes
  `test_restart_resumes_a_gate_parked_follower_instead_of_failing_it` — park a
  follower, restart, assert the run is neither `failed` nor `restarted` and still
  executes when the predecessor's queue is flushed. And D1's own statement in §7
  needs the carve-out recorded alongside the `watch_runtime` exemption it already
  documents, or the next reader re-derives the bug from the rule.

  **Do not add a marker column — derive the state from the queued message
  (2026-07-27).** The paragraph above originally prescribed writing a durable
  marker "when `submit_scheduled` returns `"enqueued"`", and that is wrong in two
  independent ways, both of which dissolve under the same correction.

  - *It has a crash window.* `_enqueue` commits the queued message in its own
    `engine.begin()` block (`core/internal_server.py:154-169`) and the route only
    surfaces to the caller after `manager.submit` returns (`:182-191`). A process
    exit between those two points leaves a committed queue row and an **unmarked**
    `running` run — precisely the case the marker exists to protect, lost to the
    write that was supposed to protect it.
  - *It has no correct retirement point.* Once the gate flushes the follower, the
    scheduled queue rows are deleted **before** `_run` starts
    (`core/session_turns.py:1287`, `messages_service.delete_queued`). A marker
    that outlives that deletion inverts its own purpose: the run is now genuinely
    mid-flight, but `recover_processing_runs` skips it and queue recovery finds no
    row to reclaim, so a crash strands it *forever* rather than terminalizing it.

  Both disappear if nothing new is written. **The queued message row is already
  the marker.** It is committed in the same transaction as the enqueue, so there
  is no window; it carries `SCHEDULED_PROVENANCE_KEY`, whose presence the code
  comment at `core/internal_server.py:148-152` already describes as marking the
  row as a scheduled segment for the flush. The rule becomes:
  `recover_processing_runs` exempts a `running` harness row **iff** a queued
  scheduled segment belonging to that run still exists. Row present ⇒ parked,
  resume it. Row gone ⇒ mid-flight, terminalize it under D1 as normal.

  **Two corrections to that rule, both required (2026-07-27).** As first written
  it claimed the two states were "mutually exclusive by construction" and keyed
  the predicate on the *session*. Both parts were wrong.

  - *There is a third state, and the row's absence does not prove dispatch.*
    `delete_queued` commits at `core/session_turns.py:1287`, but `_run` is not
    awaited until `:1423` — a long way further down the same function, outside
    that transaction. A crash in between leaves neither a queue row nor a live
    dispatch, and the rule above then terminalizes a run whose prompt never
    reached a backend. That is the exact loss D1's carve-out exists to prevent,
    reintroduced one boundary later.

    **Superseded — recording ownership is not enough (2026-07-27).** The fix
    first written here was "delete the queue row in the same transaction that
    records dispatch ownership". That does not close the window, it moves it: the
    transaction commits, then `_run` still has to reach the backend, and a crash
    in *that* interval leaves the prompt deleted with only an ownership marker
    behind. Recovery reads the marker as dispatched and terminalizes a run the
    backend never saw — so the test this section owes,
    `test_crash_between_delete_queued_and_run_does_not_discard_the_prompt`, could
    not have passed under the prescription that was supposed to make it pass.
    Ownership is a record of intent, not evidence of delivery, and no ordering of
    a destructive delete against an intent record produces evidence of delivery.

    **The prompt must survive until the backend accepts it. No existing lane
    does this — there is no precedent to copy (corrected 2026-07-27).** An
    earlier revision of this passage claimed the `agent_run` flush "does not
    delete: it *claims*". That is wrong.
    `_claim_agent_run_segment_and_retire_queue` calls
    `messages_service.delete_queued` in the **same transaction** as
    `claim_queued_runs_for_workbench_in_connection`
    (`core/session_turns.py:551-564`, delete at `:562`). The payload is destroyed
    at claim time in that lane too. What `reset_workbench_claimed_runs_in_connection`
    (`:1425-1430`) restores is the *run* rows, and the queued-row reinsert at
    `:545-548` is an in-process exception handler — both require the process to
    survive long enough for `_run` to raise. Neither survives a crash. So the
    asymmetry recorded here across two rounds does not exist at this boundary:
    both lanes delete before delivery, and copying the `agent_run` path would
    reproduce the defect rather than fix it.

    That makes the requirement heavier than previously written. **PR7 must
    introduce a durable claimed state for the message row** — retain the row with
    a claimed marker instead of `DELETE`-ing it, and retire it only on evidence
    the backend accepted the prompt. This is new mechanism, not a refactor of an
    existing one, and it should be costed accordingly.

    **The ambiguous state cannot be resolved with the dedupe I recommended
    (corrected 2026-07-27).** The previous revision proposed resuming a claimed
    row and relying on `native_message_id` uniqueness. That guard sits only on
    the *enqueue* insert: `_submit_scheduled_turn` checks `native_message_exists`
    and returns `"duplicate"` before appending (`core/internal_server.py:126-141`).
    On recovery the row already exists, so nothing consults that key — `flush_queue`
    proceeds straight to `_run` (`core/session_turns.py:1423`) and the backend is
    invoked a second time. Transcript-level dedupe downstream does not undo the
    agent side effects: a re-run can post, call tools, and spend tokens again.
    Message-row uniqueness is an enqueue guard, not backend idempotency, and I
    cited it as though it were the latter.

    So the residual ambiguity stands, unmitigated by anything currently in the
    codebase. The three durable states are `queued` (parked, resume safely),
    `claimed` (dispatch attempted, acceptance unknown), and absent (accepted);
    the middle one has no safe automatic resolution today. The genuine options
    are (a) build backend-level acceptance evidence — a positive signal recorded
    when the backend takes the prompt, which is the only thing that makes
    resumption safe; or (b) do not resume ambiguous claims: settle them `failed`
    with a distinct `interrupt_reason` and let PR6's notice surface them, trading
    a rare lost run for never duplicating agent side effects. **This is a policy
    decision and is owed its own D-number; it is not mine to settle.** (b) is
    cheaper and composes with work already planned, but it is a deliberate choice
    to lose work in a rare case, which is exactly the kind of trade that should
    be made explicitly by the maintainers rather than absorbed into an
    implementation detail.

    Deleting the payload before delivery is confirmed is the hazard, and it is
    indifferent to what stands in for the payload — a marker (round 22), a
    derived predicate (round 23), an ownership record (round 24), or a claim that
    deletes anyway (round 25) all failed at this same seam. Any future proposal
    here must name the durable artifact that holds the prompt, and the positive
    signal that retires it.
  - *The predicate must be per-run, not per-session.* With run A executing and
    run B queued on one session, a session-level test reports "a queued scheduled
    segment exists" and classifies **A** as parked too. Recovery resets both to
    held `queued`, but `recover_persisted_agent_run_queue` cross-checks the
    queue's execution IDs and can reclaim only B — so A becomes invisible to both
    recovery paths, which is strictly worse than the wrong-terminalization it was
    meant to avoid. The correlation already exists in the data: match the run
    against `task_execution_id` and the coalesced execution IDs inside
    `SCHEDULED_PROVENANCE_KEY` (`core/session_turns.py:224`, `:384`, `:463-465`;
    `_coalesced_task_execution_ids`, `core/message_dispatcher.py:41-43`). Session
    identity is not evidence of ownership and must not be used as a proxy for it.

  **Exempting is still not resuming.** Skipping the row leaves it `running`, and
  `recover_persisted_agent_run_queue` builds its eligible set only from rows whose
  normalized status is `queued` (`core/session_turns.py:1513-1517`) and then keeps
  only those carrying `workbench_queue_holds_run`
  (`_run_metadata_holds_workbench_queue`, `:1518-1521`). A scheduled/watch
  follower satisfies neither: it is `running`, and the hold marker is written only
  on the `agent_run` requeue path (`core/scheduled_tasks.py:2817`) — the
  scheduled/watch site discards the route entirely. So widening the trigger-kind
  and run-type predicates, as the round-20 note prescribed, still yields an empty
  eligible set and silently leaks the follower. Recovery must additionally
  **reset the exempted row to `queued` and stamp the hold metadata** before
  delegating — or define explicit claim semantics for marked `running` rows. The
  reset is the smaller change and matches what the `agent_run` path already does
  at `:2817`; it must happen in the same transaction as the exemption decision, or
  the crash window simply moves.

  Owed alongside the test above:
  `test_crash_between_enqueue_commit_and_dispatch_leaves_follower_recoverable`,
  `test_flushed_follower_is_terminalized_not_skipped_after_restart`,
  `test_crash_between_delete_queued_and_run_does_not_discard_the_prompt` (the
  third state), and
  `test_running_run_is_not_parked_by_a_sibling_queued_segment` (per-run
  correlation — assert A stays claimable, not merely that B resumes). The last
  two are the cases the first version of this rule got wrong; without them the
  rule reads as if the classification were total when it is not.

  One more, covering the supersession above:
  `test_crash_after_ownership_write_but_before_backend_accept_retains_the_prompt`
  — assert the claimed row is still **present**, which is the assertion that
  fails under every version of this rule proposed before round 24. Note that it
  asserts retention only, not resumption: whether a recovered `claimed` row is
  resumed or failed is the open policy decision above, so a test asserting
  "resumable" would presuppose one of the two answers. The resumption half of
  this test can only be written once that D-number is decided.

  Either way PR7 owes
  `test_gate_queued_harness_follower_is_not_failed_by_the_orphan_sweep` — park a
  scheduled run behind a live turn, advance past `orphan_grace_seconds`, and assert
  it still executes when the predecessor completes.

  So the layering is:

  - **fast path** — the in-band retry, best-effort. When the DB recovers a moment
    later this settles the row promptly with the real result text, which is the
    good outcome and worth keeping. It is an optimization, not the guarantee.
  - **guarantee** — the sweep. A run left `running` with no live execution is
    settled from the row itself — **but the row is not the only durable
    evidence, and failing without consulting the rest fabricates a failure
    (corrected 2026-07-29, review).** The recorder's terminal write can fail
    while the `persist_agent_message` a moment later succeeds: the user then
    holds a delivered result and the DB holds a `messages` row carrying the
    terminal text and the run's provenance (`task_execution_id` / the
    coalesced execution ids / the stable output id). Sweeping that run to
    `failed` + `interrupt_reason` records a run the user watched succeed as a
    failure — and PR6 then notifies them about it. So the sweep **reconciles
    first**: correlate persisted terminal output by run provenance, and when a
    matching terminal receipt exists, settle the run from the receipt — real
    status, real text. Only when *neither* the run write nor a terminal
    receipt exists does it fall back to `interrupt_reason` marking that no
    terminal payload was recoverable, surfacing through PR6 as a visible
    failure — and PR7 must state the result text may be **lost** in that
    residual case; pretending otherwise would repeat the over-claim pattern of
    §5. Owed test:
    `test_sweep_reconciles_a_persisted_terminal_receipt_before_failing_the_run`.
  - **not a guarantee** — PR6's dead letter, for this path. It presumes a
    persisted marker, so it covers a failed *delivery*, not a failed *terminal
    write*.

  PR7 owes four tests, because the failure has more victims than the previous
  revision accounted for:
  `test_transient_db_error_on_terminal_write_does_not_strand_the_run` (force the
  write to raise; the run still reaches a terminal status);
  `test_terminal_write_failure_still_releases_the_turn_waiter` (same forced
  failure; `dispatch_turn_with_outcome` returns rather than hanging — the first
  alone would pass against the propagation shape while the process quietly
  deadlocks); `test_retry_enqueue_failure_still_leaves_the_run_recoverable` (fail
  the terminal write **and** the retry enqueue; the sweep still settles it); and
  `test_crash_between_waiter_release_and_retry_enqueue_is_recovered_on_restart`
  (no marker was ever written; `recover_processing` settles the row at init). The
  last two are the cases this correction adds — a one-shot write failure, which is
  all the previous tests exercised, passes against a design that fails both.
- **result-less endings**: `settle_agent_runs_without_result`
  (`core/scheduled_tasks.py:3038`, called from `core/session_turns.py:827`) keeps
  its current job and only that job — the cases where no terminal result is
  coming, which is what `settled_by` is sufficient to express.
- **`mark_task_result`**: the recorder is a run-row writer and task health is a
  separate concern, so this needs its own terminal-time update rather than being
  folded in — but it must fire on **both** lanes, or `vibe task list` stays
  dishonest for precisely the Workbench runs this PR is about.

Regression coverage must be avibe-targeted, not just IM-targeted:
`test_scheduled_avibe_run_settles_at_terminal_result_not_at_gate_submit` — submit
a scheduled run at an avibe session, assert the run stays `running` across
`submit_scheduled` returning, and only reaches a terminal status when the gated
turn produces its result. The IM-lane test alone would pass against the broken
gate path, which is how this gap survived into the plan. Add
`test_scheduled_avibe_run_has_exactly_one_terminal_writer` — assert the turn-lane
helper does **not** also write a terminal status for a run the outbound recorder
already settled, which is the failure mode the retraction above avoids.

**Historical rows are PR7's job too (D6, assigned 2026-07-27).** This section
previously said historical rows "keep their (wrong) values" and claimed no UI/i18n
work, which left D6 owned by nobody — PR1–PR6 do not touch it either. That is not
a deferral, it is a regression PR7 itself creates: the ~144 legacy rows are
distinguishable from honest ones *today* only because **every** row is dishonest.
The moment PR7 lands, they become indistinguishable, and history quietly lies. So
D6 ships here:

- one-shot `UPDATE` stamping `metadata.pre_settlement_migration = true` on the
  rows matching the **premature-success signature**, not on every pre-cutover
  `scheduled`/`watch` row (corrected 2026-07-29, review): `status='succeeded'`
  AND empty `result_text` AND the dispatch-time completion signature
  (`completed_at` within seconds of `created_at`). Pre-cutover rows that failed
  synchronously, were explicitly canceled, or otherwise carry an honest
  terminal outcome are left unannotated — a "legacy — delivery only" marker on
  a genuine failure would rewrite unrelated history. (No schema change.)
- a quiet "legacy — delivery only" marker in the CLI run views and the UI run
  detail, reading that flag — which does mean PR7 carries **one** i18n string
  pair (`vibe/i18n/` + `ui/src/i18n/{en,zh}.json`).

If PR7 gets split for review size, the stamp and the marker must land in the
**same** release as the settlement change, not a follow-up.

**Two safety mitigations ship in the same PR — without them PR7 is a regression:**

*(Scope note, 2026-07-27: the acknowledgement protocol derived under the first
mitigation below is **implemented by PR6**, not here — PR7 only stamps
`owed_failure_notice=pending` and relies on PR6's drain, which by then
already exists. §5 ownership correction has the reasoning.)*

- **Restart must not re-dispatch (D1).** Otherwise `recover_processing_runs`
  becomes a duplicate-prompt generator: a mid-flight daily report re-sent after
  restart posts twice. Recovered `scheduled`, `watch` **and** `agent_run` rows
  terminalize with `interrupt_reason=restarted`; `watch_runtime` stays exempt via
  the `run_type != "watch_runtime"` filter already at
  `storage/background.py:2202`. (`watch` is included deliberately — see the D1
  correction in §7: a `watch` run carries an arbitrary agent prompt.)

  **Second exemption — gate-parked followers (2026-07-27).** A run parked behind
  a live Avibe turn is also `running`, but it has **not** dispatched: no prompt
  reached a backend, and the durable queue row is what would deliver it. D1's
  duplicate-prompt premise does not apply, so terminalizing it prevents nothing
  and destroys work that was always going to run. `running` is a proxy for "was
  mid-flight" and the gate handoff is where the proxy breaks — there it means
  "accepted, not yet started". So `recover_processing_runs` exempts a `running`
  harness row **iff** a queued scheduled segment **belonging to that run** still
  exists — correlated by `task_execution_id` and the coalesced execution IDs in
  `SCHEDULED_PROVENANCE_KEY`, never by session identity, which would classify a
  genuinely running run as parked whenever a sibling is queued behind it. Derive
  this from the durable queue row rather than a separately written marker, and
  require that the row survive **until the backend accepts the prompt** — not
  merely until dispatch ownership is recorded, since ownership is intent rather
  than delivery. `_run` is awaited at `core/session_turns.py:1423`, long after
  `delete_queued` commits at `:1287`, so absence of the row does **not** prove
  the prompt was dispatched. There is no lane to copy: the `agent_run` flush
  deletes the payload in the same transaction as its claim
  (`core/session_turns.py:551-564`, delete at `:562`), and its restore paths
  (`:545-548`, `:1425-1430`) are in-process exception handlers that no crash
  survives. PR7 must therefore introduce a **durable claimed state** for the
  message row — new mechanism, not a refactor — which leaves a third state,
  claimed with acceptance unknown, whose resolution is a policy decision owed its
  own D-number and not settled in this plan. §4 has the derivation and the two
  genuine options.
  Exempting is not resuming: the row must also be reset to `queued` with the hold
  metadata stamped, in the same transaction, or the widened queue recovery will
  not see it. Note the phase gap that makes this easy to get wrong: this routine
  runs inside `ScheduledTaskService.__init__` (`core/scheduled_tasks.py:1713`),
  whereas the queue recovery that would resume the row runs strictly later, from
  `core/internal_server.py:724-727`. See the gate-follower resolution in §4 for
  the full derivation.

  **Correction (2026-07-27) — the restart path needs its own notification, and
  depending on PR6 does not give it one.** `recover_processing()` is called
  **synchronously from `ScheduledTaskService.__init__`**
  (`core/scheduled_tasks.py:1713`). The row is terminal before the service is
  even running, so it never re-enters `_execute_task` — which is the only place
  PR6 specifies a notification. A restart-terminalized run with no
  `callback_session_id` would therefore be silently failed *forever*: not
  executed, not notified, not revisited. Rows that *do* have a callback target
  are fine — `_drain_callbacks` picks them up on the 2 s tick — so this gap is
  exactly the no-callback class from PR2 correction 2, reached by a different
  road.

  Two ways to close it:

  - **(a) Return the affected rows.** `recover_processing_runs` hands its
    recovered ids back; `__init__` stashes them and a startup drain notifies.
    Simple, but the notice lives only in memory — a crash between terminalize and
    notify loses it, and this code path exists *because* the process crashed.
  - **(b) Persist an owed notice.** Stamp `metadata.owed_failure_notice`
    during the same guarded UPDATE that terminalizes, and drain it from the
    existing 2 s tick alongside `_drain_callbacks`. Survives the crash, reuses
    the drain that is already there, and `__init__` stays synchronous.

  **(b) is the recommendation**, and it generalizes: the eviction and
  lifetime-timeout paths can stamp the same field instead of each growing its own
  delivery. That makes the "interrupted run notification" one drain rather than
  three call sites, and turns PR2 correction 2's dependency on PR6 into a
  dependency on *this* stamp — which PR6 then renders.

  **Acknowledgement protocol (2026-07-27).** ⚠️ **Owned by PR6, not PR7** — it is
  derived here because the restart path is what forced it, but PR2 depends on PR6
  and stamps `pending` notices, so PR6 must land the whole drain (receipt, ack,
  backoff, dead letter, structured result, `persist_agent_message` error channel)
  or PR2 ships notices nothing can deliver. See the ownership correction in §5.
  PR7 only *stamps*. — "Persist a marker" is not yet a
  design — a bare boolean has no crash-safe order of operations. Clear it *before*
  `emit_backend_failure` and a crash mid-delivery loses the notice for good;
  clear it *after* and a crash between delivery and the clear re-sends it. The
  plan therefore specifies all three of the following, and PR6 is not done until
  the third is tested.

  1. **State, not boolean.** `owed_failure_notice` is
     `pending` → `sent`, mirroring the `callback_status` vocabulary already in use
     (`pending`/`sent`/`skipped`/`failed`, written through
     `update_callback_status` at `core/scheduled_tasks.py:1272`). Same shape, same
     drain, nothing new to learn. `skipped` covers a row the renderer decides
     needs no user-visible notice — **including streak suppression**, which is
     the drain declining to notify because an earlier notice in the same
     consecutive-failure streak was already acknowledged (see PR6 step 5); it is
     terminal and never retried. `failed` covers a delivery that errored, and
     stays visible rather than silently retrying forever.
  2. **Acknowledge on a durable receipt, never on a function return.**
     *(Corrected 2026-07-27 — the previous revision said "flip to `sent` once
     `emit_backend_failure` returns", which is wrong.)* The `notify` branch
     (`core/message_dispatcher.py:1668-1686`) catches a send failure, logs it and
     returns `None`; `emit_backend_failure` (`core/backend_failure.py:152-160`)
     awaits `emit_agent_message` in a `try/finally` and **discards the result**,
     returning normally either way. A returns-cleanly ack would therefore flip to
     `sent` on a delivery that never happened, permanently losing the D1 notice —
     the precise silent failure this whole section exists to prevent.

     The receipt is the **persisted `messages` row**. That is a sound signal
     because the notify branch only calls `persist_agent_message` when
     `message_id is not None` (or the transport `persists_without_delivery`), so
     a row implies the send returned an id.

     **Delivery and persistence are two signals, and PR6 must surface both.**
     *(Added 2026-07-27 — the previous revision offered "return the message id"
     and "re-read the row" as interchangeable alternatives. They are not, and
     neither one alone is sufficient.)* `persist_agent_message` swallows its own
     failures and returns `None` (`core/message_mirror.py:485-488`), and the
     notify branch **discards that return value** (`:1671-1677`). So the message
     id alone can be present with no row behind it, and a row-only check cannot
     distinguish "never delivered" from "delivered, bookkeeping failed". Thread
     a structured result — `(delivered_id, persisted_row, error)` — out of the
     notify branch and branch on it.

     **The `error` member is not optional.** *(Added 2026-07-27 — the previous
     revision specified only the pair, which cannot support the dead-letter state
     it also promised.)* `except Exception as err: logger.error(...)` then
     `return None` (`:1682-1686`) is the only place the delivery failure exists;
     it is logged and dropped. A bare pair therefore collapses every failure to
     `(None, None)` and leaves the drain with nothing to put in `error`, so PR6
     would surface a dead letter that cannot say *why* — the actionable-failure
     requirement in D1 reduced to "something went wrong". Either the caught
     exception is propagated in the result or it is allowed to reach the drain.

     A second reason the pair is insufficient: the `try` also covers
     `_stream_chunk` (`:1681`), so `(None, None)` can mean "delivered **and**
     persisted, but the SSE stream raised afterwards". Without the error the drain
     cannot tell that from a failed send, and would re-send an already-delivered
     notice. The structured result must let the drain distinguish *where* the
     failure happened, not merely that one did.

     **The persistence error is dropped the same way, and must be surfaced too.**
     *(Added 2026-07-27 — the outcome table below promised "`error` recorded for
     diagnosis" on the delivered-but-unpersisted row while the only available
     signal was a bare `None`, so that row could not have been implemented as
     written.)* `persist_agent_message` ends in `except Exception:` — the
     exception is not even bound to a name — then `logger.exception(...)` and
     `return None` (`core/message_mirror.py:486-488`). Propagating that return
     value tells the drain a receipt is *missing* and nothing about why. So the
     persistence layer must surface its caught exception to its caller as well.

     **But it must not simply be allowed to raise**, which is the tempting
     one-line version: the notify branch's blanket `except Exception` (`:1682`)
     would catch it and `return None`, **discarding the `message_id` already
     assigned on the line above** — turning delivered-but-unpersisted into
     looks-like-never-delivered, i.e. converting outcome row 2 into row 3 and
     resending a notice the user already has. That is the exact bug this
     subsection has now been corrected for three times, so the mechanism is:
     `persist_agent_message` returns the error alongside the row (or accepts an
     error sink) **without raising through the notify branch**, and PR6 owns
     restructuring that branch so a persistence failure cannot eat the delivery
     id. Note this widens PR6's blast radius: `persist_agent_message` has 12
     non-test call sites, so the added channel must be backward-compatible for
     callers that ignore it.

     Given a result that carries all three, the outcome table is:

     | delivered | persisted | outcome |
     |---|---|---|
     | ✅ | ✅ | `sent`. The normal path — `error`, if any, is post-delivery (e.g. `_stream_chunk`) and must not trigger a resend. |
     | ✅ | ❌ | **`sent`, with `ack_evidence="delivery_only"`** + `error` recorded for diagnosis. |
     | ❌ | ❌ | stay `pending`, backoff, bounded (below); **`error` is what the eventual dead letter reports.** |

     The middle row is the one that matters, and it is deliberately **not** a
     retry. D1's requirement is that the user is *told*; a returned message id is
     positive evidence they were. Re-sending because the DB write failed would
     spam a notice that already arrived — the "every 2 s forever" failure mode.
     The cost is losing the dedup record for that notice, which only widens the
     already-accepted duplicate window in (3), so the ack records
     `ack_evidence` to keep it visible rather than pretending the receipt exists.

     **Bounded retry applies only to the no-evidence row**, and it is new
     machinery: there is no attempt counter or backoff anywhere in
     `core/scheduled_tasks.py` today, so PR6 introduces `attempts` +
     `next_attempt_at` on the notice and the drain skips a notice whose backoff
     has not elapsed. Exponential from the 2 s tick, capped; on exhaustion the
     notice goes `failed` with the last error — the `error` member above, which is
     why it has to be in the result — and surfaces through PR6, a visible dead
     letter rather than a silent drop or an infinite loop. Retries
     are safe to attempt because `agent_message_exists` (defined in
     `core/message_mirror.py:491`, called from `core/message_dispatcher.py:1547`)
     already guards the send path against re-posting an identity that did persist.
  3. **Make the identity stable, and pick the failure direction on purpose.**
     The drain passes `failure_id=f"interrupt:{run_id}:{interrupt_reason}"`
     explicitly. The identity must not depend on what context the drain happens
     to construct: this path runs at startup with no live request, so the drain
     rebuilds a context from the run row, and `_failure_identity`
     (`core/backend_failure.py:31-57`) would otherwise key off whatever
     `task_execution_id` that rebuild supplies — stable only if the drain
     re-derives it from the durable row, and `uuid.uuid4().hex` if it does not.
     An explicit run-derived id removes that coupling entirely. It also keeps the
     interrupt notice distinct from an ordinary backend-failure notice for the
     same execution, which a bare `task_execution_id` would collide with and
     silently suppress.

     With a stable id the existing chain deduplicates: `idempotency_key =
     f"backend-failure:{identity}"` (`:85`) → `output_id`
     (`core/message_dispatcher.py:1076`) → `messages.native_message_id`, unique
     under `UniqueConstraint("platform", "native_message_id")`
     (`storage/models.py:372`).

     **That is exactly-once *persistence*, not exactly-once delivery, and the
     plan should stop claiming otherwise.** *(Corrected 2026-07-27 — the previous
     revision claimed "exactly-once user-visible".)* There is a real window
     between `im_client.send_message` succeeding and `persist_agent_message`
     writing the row (`core/message_dispatcher.py:1668-1680`): a crash inside it
     leaves the user with a delivered message and the DB with nothing to
     deduplicate against, so the next drain sends it again. Closing that window
     needs provider-side idempotency or a real outbox (record intent → send →
     mark sent), and **PR6 does neither** — the seam is
     interruption-notification-shaped, not messaging-infrastructure-shaped.

     So the guarantee is stated honestly as **at-least-once delivery with a
     bounded duplicate window**, and the direction is chosen deliberately: a
     duplicated "your run was interrupted" notice is a papercut, a lost one is
     the D1 violation. If a real outbox lands later, this mechanism inherits
     exactly-once for free, because the identity is already stable.

  **These are claims until tested, and they are the whole mechanism.** PR6 owes:
  (a) a crash between delivery and the `sent` flip → the next drain finds the
  receipt row and acknowledges without re-sending, exactly one `messages` row for
  that `(platform, native_message_id)`; (b) a send that fails or returns no id →
  the notice stays `pending`, is retried, and is **not** marked `sent` (the
  regression test for correction 2); (c) two drain passes over the same recovered
  row produce one identity, pinning that the id is re-derived from the durable run
  row rather than from a per-pass context; (d) **delivered-but-unpersisted** —
  `send_message` returns an id, persistence raises — acks as `sent` with
  `ack_evidence="delivery_only"`, sends **exactly once**, records the persistence
  exception's own message, and does **not** lose the delivery id; (e) **bounded
  retry** — a persistently failing send backs off rather
  than firing every tick, and terminates in `failed` carrying the raised
  exception's own message rather than a generic string; and (f) **a post-delivery
  error is not a delivery failure** — send and persist succeed, `_stream_chunk`
  raises, the notice still acks and is **not** resent. Only with these passing may
  the mechanism be described as crash-safe — and even then, only at the
  at-least-once guarantee stated above.

  *(A previously prescribed negative test — "omit the explicit `failure_id`,
  assert two rows" — is dropped as unsound: `_build_context` always populates
  `platform_specific["task_execution_id"]` (`core/scheduled_tasks.py:3335`), which
  `_failure_identity` consults before its `uuid4` fallback, so a
  production-shaped context yields one row and the test would only have proven
  something about an artificial context the drain never builds. Test (c) pins the
  property that actually matters.)*
- **The cron must not be blockable (D4).** Today `_run_task` awaits the execution
  (`:2004-2005`) under `max_instances=1` (`:1946`); with a PR7-length turn a hung
  run silently discards every subsequent fire. Fire becomes enqueue-only, plus a
  per-run lifetime cap honoring `run_definitions.lifetime_timeout_seconds`
  (currently watch-only) defaulted to `min(configured, 0.8 × cron_interval)`.
  There is **no turn-duration timeout anywhere by design**
  (`core/services/dispatch.py:116-118`), so this cap is the only backstop.

  **The cap must not be attached to the scheduler execution, or it misses the
  avibe lane entirely (added 2026-07-27).** For an avibe-targeted scheduled/watch
  run, `_execute_request` returns immediately after `submit_scheduled`
  (`core/scheduled_tasks.py:3258-3261`), so the scheduler execution disappears
  while the real work either runs under `SessionTurnManager` or sits durably
  queued behind a predecessor. A timeout hung off `_run_task` or
  `_execute_claimed_request` therefore expires against an execution that has
  already returned, cancels nothing, and leaves the cron blocked in precisely the
  lane PR7 moves harness work into — so the cap would appear to exist while
  protecting only IM targets.

  The cap must instead be a **run/session watchdog that survives the handoff**:
  keyed on the run and its target session rather than on the scheduler task, armed
  when the run is dispatched, disarmed when the run reaches a terminal status from
  either lane.

  **And it bounds inactivity, not turn duration (corrected 2026-07-29,
  review).** An absolute cap expiring at `0.8 × cron_interval` would cancel a
  healthy turn that is still streaming assistant/tool events — precisely the
  turn-duration timeout `core/services/dispatch.py:118-121` forbids and PR3's
  retraction re-derives; short-period tasks would have normal executions killed
  as `lifetime_timeout`. The watchdog therefore expires only when the cap has
  elapsed **with no observable progress**: while the run is queued or
  gate-parked the clock runs, and once the turn is live, observable progress —
  the same real assistant/tool traffic that legitimately bumps
  `session_last_activity` (PR3's bar) — re-arms it. A hung predecessor shows no
  progress and is cancelled at the cap, which is the case D4 exists for; a
  healthy long turn shows progress and is exempt, and the next fire queues
  behind it under D4's enqueue-only rule rather than being discarded. The
  reserved name `lifetime_timeout` (`core/run_settlement.py:14`) keeps its
  meaning: the run's *unproductive* lifetime hit the bound.

  On expiry it records `interrupt_reason=lifetime_timeout` **before**
  cancelling — the same record-the-cause-first rule as PR2 step 2 and the
  manager-lane cancel — then invokes the **cause-aware manager cancellation** from
  PR2 rather than `SessionTurnManager.cancel`, so the run settles `failed` with
  that reason and not `canceled` as a user Stop. It must also handle the
  **still-queued** case: a follower parked behind a live turn has no manager turn
  to cancel, so the watchdog retires the gate segment and terminalizes the row
  directly, or the cap silently does not apply to exactly the runs most likely to
  be waiting a long time.

  Owed tests: `test_lifetime_cap_cancels_an_avibe_targeted_run_and_unblocks_the_cron`,
  asserting `interrupt_reason == "lifetime_timeout"` — an IM-targeted test passes
  against a scheduler-attached timeout that never fires on the gate lane — and
  `test_lifetime_cap_does_not_cancel_a_turn_with_observable_progress`, the
  invariant's own guard: a turn streaming past the cap must survive.

**Rejected alternative:** adding a `dispatched` status or `dispatched_at` field.
The enum has no DB constraint, six derived predicates key off the current five
values, and the UI renders the raw string — an untranslated `dispatched` would
ship visibly. Not worth it unless the product genuinely wants both facts surfaced.

**Also rejected for now:** extending the durable-queue seam
(`session_turn_gate`) to IM platforms so delivery is at-least-once end-to-end.
That is the eventual convergence target for P4, but it changes `messages`
persistence, dedupe-by-`native_message_id`, and queue visibility for five
platforms — the same "broader product migration" the terminal-lifecycle doc
deferred.

## 5. Dependency order

```
PR1 (P2 capture)          — MERGED (#1063)
PR5 (P5 bindings)         — MERGED (#1064); shares the notify hook with PR6
  └─ PR6 (P6 visibility)  — IN REVIEW (#1072); same choke point as PR5's pause; OWNS THE WHOLE
       │                    owed-notice drain: renders it AND implements the
       │                    receipt/ack/backoff/dead-letter protocol plus the
       │                    (delivered_id, persisted_row, error) result and the
       │                    persist_agent_message error channel it needs
       └─ PR2 (P3 reconcile) — stamps metadata.owed_failure_notice
            │                  (correction 2: a terminalized run with no
            │                  callback_session_id is otherwise silent,
            │                  violating D1); provides the session→runs resolver
            └─ PR3 (P4 interlock) — reuses that resolver, so it is a CHILD of PR2,
                 │                  not its sibling (corrected 2026-07-27: the
                 │                  tree drew both as PR6-only children, which
                 │                  reads as "either order", while §3.4 requires
                 │                  the resolver be built once — PR3 first would
                 │                  either duplicate it or stall)
                 └─ PR4 (P4 drain) — own review, own PR
PR7 (P1 settlement)       — needs PR1 landed, PR6's drain available, AND PR2's
                            periodic reconcile. PR7's recovery guarantee is a
                            sweep, and NEITHER existing one provides it — these
                            are two different functions and an earlier revision
                            described them as one "init-only and agent_run-only"
                            sweep (corrected 2026-07-27):
                              - recover_processing_runs (background.py:2195-2220)
                                is init-only (called once from
                                scheduled_tasks.py:1713) but NOT agent_run-only —
                                it requeues every non-watch_runtime running row
                                back to queued, terminalizing nothing;
                              - sweep_stale_runs' orphan branch (:2421-2427) is
                                agent_run-only by an explicit comment, but runs
                                periodically, not at init;
                            so PR7 widens the :2425 orphan predicate to the
                            harness run types AND relies on PR2's reconcile. It
                            only
                            STAMPS the owed notice from the restart path, which
                            never reaches _execute_task at all (see PR7's
                            restart correction); also carries D6's history
                            stamp + legacy marker, and widens the :2425 orphan
                            predicate to the harness run types
```

**The reordering is the main structural change from the 2026-07-27 pass.** PR2
was previously second; it is now gated behind PR6, which pushes PR3 and PR4 back
with it.

**Ownership correction (2026-07-27).** The drain protocol used to be assigned to
PR7 while PR2 depended only on PR6 — which would have let PR2 ship *writing*
`pending` owed notices before anything could reliably deliver them, missing its own
exit criterion ("a delivered user-visible notice, not a terminal row") in exactly
the no-callback case correction 2 exists for. The attempt counter, backoff,
terminal handling, structured delivery/persistence result and
`persist_agent_message` error channel are therefore **PR6's**, not PR7's. PR6 is
the prerequisite PR2 already declares, so the graph edge stays as-is and becomes
true rather than aspirational; PR7 is reduced to *stamping* from the restart path
and consuming a drain that already exists. (Making PR2 depend on PR7 instead was
the alternative — rejected because PR6 is already the "tell the user about
failures" PR, so the drain belongs with the renderer that reads it, and because it
would serialize PR3/PR4 behind PR7 for no reason.)

The **owed-notice stamp** is the shared seam that makes this work: PR2 and PR7
both write `metadata.owed_failure_notice` inside the guarded UPDATE that
terminalizes, and one drain on the existing 2 s tick — PR6's — renders it. Without it,
"depend on PR6" is an empty edge for two of the three interruption paths —
restart terminalizes from `ScheduledTaskService.__init__` and eviction
terminalizes out of band, and neither ever reaches the `_execute_task` hook PR6
specifies.

All six product decisions are resolved (§7). The owed notice is **persisted**,
with the acknowledgement protocol spelled out under PR7's restart correction (where
it was first derived) but **implemented by PR6** per the ownership correction above —
`pending`/`sent`, acknowledged on either valid evidence of delivery (a persisted
`messages` row, or a delivered id whose row write failed — never a bare function
return), deduplicated by a stable run-derived `failure_id`, bounded-retry only when
there is no evidence of delivery at all, and honest that the guarantee is
at-least-once delivery rather than exactly-once. The in-memory variant is
documented there as the rejected alternative.

**One implementation choice remains open, and it is load-bearing — it is now
D7 in §7.** This section previously ended "No implementation choices remain
open." That was false at the time it was written: §4 defers whether a
crash-recovered `claimed` message row is resumed or failed, and it cannot be
settled by an implementer because both answers lose something real — resuming
can duplicate agent side effects (posts, tool calls, spend), failing can discard
work the backend never received, and the two states are indistinguishable from
the durable record. **PR7 is therefore not fully specified and must not be
treated as approved until that D-number is added and decided by the
maintainers.** The dependency status in §5 and the recovery tests in §6 both
follow from the answer, not the other way round. Every other section of this
plan is closed; this one is not, and saying so is more useful than a declaration
that reads as complete.

## 6. Test plan

All hermetic. `tests/conftest.py:53-72` `_isolate_vibe_remote_home` is autouse and
repoints `Path.home()`, `HOME`, `AVIBE_HOME`, `XDG_*`, `CODEX_HOME`,
`CLAUDE_CONFIG_DIR` into `tmp_path`; `_reset_cached_sqlite_engines` (`:75-85`)
prevents engine bleed. **Do not** add `@pytest.mark.uses_real_paths`.
Use `ensure_sqlite_state(db_path=tmp_path/…)` so Alembic runs — `metadata.create_all`
alone will **not** reproduce P5 symptom 2 (§2 P5, drift #3).

**`tests/test_message_dispatcher_scheduled.py`** (PR1) — every existing context
uses `task_trigger_kind: "agent_run"`. Add `"scheduled"` variants of
`test_visible_agent_run_result_marks_run_terminal` (L1023, the `post_to='channel'`
case) and `test_suppressed_agent_run_result_marks_run_terminal` (L680).

**`tests/test_scheduled_tasks.py`** (5195 lines, the natural home):
- PR2: `test_cancelling_an_inflight_execution_terminalizes_and_frees_the_session_lock`
  — cancelling an in-flight execution **terminalizes** the run (D1, not the
  pre-D1 requeue this line used to describe) **and** discards its
  `_inflight_sessions` lock so the next drain dispatches. The wedge regression, and
  the highest-value new case; it was the one owed test in this list carrying no
  name (named 2026-07-27), which in a list of ~91 named tests made the most
  important one the only unverifiable entry. There is no retry-counter case: D1 dropped
  the attempt ceiling along with the requeue. Add two more:
  `test_service_stop_terminalizes_a_scheduled_run_instead_of_requeueing` (PR2
  correction 1 — the duplicate-prompt regression) and
  `test_evicted_scheduled_run_without_a_callback_target_still_notifies`
  (correction 2 — guards against the silent terminal row). The cancellation scan
  needs **both** join paths covered, because they share no code:
  `test_cancel_session_executions_finds_a_pinned_session_execution_via_lock_owners`
  (the ordinary case — session id → lock key → `_session_lock_owners` → run id)
  and `test_cancel_session_executions_finds_a_create_per_run_execution` (no lock
  key exists, so only the `run_id → session_id` map can find it). Round 13's scan
  list would have passed the second and failed the first. And the reason must be
  pinned per source, not assumed:
  `test_cancellation_cause_distinguishes_eviction_from_shutdown` — evict one run,
  stop the service under another, assert `interrupt_reason` is `evicted` and
  `restarted` respectively. A single-source test passes trivially against the
  hard-coded string this round removed.
- PR3: `test_claimed_request_does_not_refresh_session_last_activity` (the
  item-2 correction: a queued claim must not manufacture liveness);
  `test_pending_agent_run_pins_target_session_against_idle_eviction`;
  `test_unresolvable_session_id_does_not_pin_a_session`; and the two that guard
  the immortal-session trap from the item-2 correction —
  `test_gate_wait_does_not_refresh_session_last_activity` (a successor blocked on
  `gate.lock` must not bump the clock) and — **inverted from the previous
  revision** — `test_long_streaming_turn_is_not_evicted_by_the_stuck_ceiling`
  (a turn far older than 1800 s that is still emitting assistant/tool events must
  **survive**, guarding the `dispatch.py:118-121` no-turn-duration-timeout
  invariant). The earlier prescription asserted the opposite and would have locked
  in a 30-minute turn cap.
- PR4: `test_drain_rearms_when_a_pending_request_is_skipped`;
  `test_watch_store_tick_is_not_blocked_by_a_hung_recovered_activity_delivery`
  — the direct regression test for the field incident.
- PR5: `test_execute_task_pauses_and_notifies_when_pinned_session_is_missing`;
  `test_create_once_rebinds_when_session_deleted`;
  `test_existing_policy_never_rebinds`; `test_repeated_failures_do_not_notify_twice`;
  `test_rebind_preserves_model_of_the_deleted_session` — delete a session whose
  `model`/`reasoning_effort` differ from the scope agent's current values, then
  rebind, and assert the execution runs on the *old* settings. Without the §PR5.1
  snapshot this test cannot pass, which is the point: it is the executable form of
  D3.
- PR7: clone `test_agent_run_stays_running_until_terminal_result` (`:2054`) as
  `test_scheduled_run_stays_running_until_terminal_result`. **`:4098`
  `test_drain_requests_records_scheduled_create_per_run_reserved_session`
  asserts `payload["ok"] is True` immediately after drain (`:4148-4151`) — it
  locks in the bug and must be updated.** Same shape at `:4039` for watch. The
  corrected assertion, which the previous revision left unstated: after drain the
  run is **still nonterminal** and `ok` is absent or false, and `ok is True`
  appears only after a terminal result is recorded — inverting the current
  assertion rather than deleting it, so the test keeps failing if the premature-`ok`
  behaviour returns.
  Plus `test_restart_does_not_requeue_a_midflight_scheduled_run_into_a_duplicate_prompt`
  — the D1 restart case, named here 2026-07-27 (it was prose only) — and note its
  companion carve-out `test_restart_resumes_a_gate_parked_follower_instead_of_failing_it`
  is the *other* half: one asserts a mid-turn run is not re-dispatched, the other
  that a parked follower still is. A test suite with only the first would make the
  §7 D1 carve-out unenforced.
- **PR6**, owed-notice drain — these ship with the drain itself, *before* PR2 can
  stamp its first `pending` notice (see the §5 ownership correction; this list was
  previously filed under PR7, which would have left PR2's exit criterion untested
  at the time PR2 landed). The set that licenses the word "crash-safe":
  `test_owed_notice_acked_from_receipt_after_crash_before_flip` (kill after
  `emit_backend_failure`, before the `pending`→`sent` flip; the next drain finds
  the persisted receipt row and acknowledges without re-sending — exactly one
  `messages` row for that `(platform, native_message_id)`);
  `test_owed_notice_stays_pending_when_notify_delivers_nothing` (stub
  `im_client.send_message` to raise, so the notify branch at
  `core/message_dispatcher.py:1668-1686` returns `None` — assert the notice is
  **not** `sent`, is retried, and eventually lands; the regression test for
  acking on a function return);
  `test_owed_notice_identity_is_stable_across_drain_passes` (two passes over the
  same recovered row → one identity, pinning that the id comes from the durable
  run row and not from a per-pass rebuilt context);
  `test_owed_notice_acks_when_delivered_but_persist_fails` (make the persistence
  layer **raise** — not merely return `None` — while the send returns an id →
  `sent` with `ack_evidence="delivery_only"`, **one** send rather than a re-send
  loop, **and the recorded `error` contains that exception's message**. Stubbing a
  bare `None` would pass without proving the diagnosis half, since
  `core/message_mirror.py:486-488` discards the exception unbound. Also assert the
  delivery id survives, which is the regression guard for letting persistence
  raise through the notify branch's `except`); and
  `test_owed_notice_retry_backs_off_and_dead_letters` (a persistently raising
  `send_message` → attempts are spaced by `next_attempt_at` rather than firing on
  every 2 s tick, and the notice ends `failed` carrying **the raised exception's
  message**, not a generic string — the regression test for the error being
  dropped at `core/message_dispatcher.py:1682-1686`); and
  `test_owed_notice_does_not_resend_when_post_delivery_stream_fails` (send and
  persist both succeed, `_stream_chunk` (`:1681`) raises → still `sent`, one send,
  proving the drain distinguishes a post-delivery error from a failed send).

**`tests/test_claude_cli_path.py`** (PR2/PR3) — all 10 existing
`evict_idle_sessions` tests live here (`:1323-2130`), with a stub `_Controller`
(`:68-92`) and frozen `time.monotonic`: eviction with an in-flight run calls
`cancel_session_executions`; without one, no spurious DB hit; bounded exemption
below/at the ceiling.

**New `tests/test_session_idle_eviction_interlock.py`** — pinned/unpinned, pin
appearing *between* the two passes, a single unresolvable binding (fails open:
that binding does not pin), and the provider itself raising (fails closed: the
eviction pass aborts and no session is evicted on missing safety data).

**`tests/test_inbox_events.py`** (PR2) — reconcile on a `running` row →
**`failed`** (not `canceled` — see the §3.3 correction and the HFR-012/HFR-037
guardrails) + `completed_at` + `callback_status` still `pending`, then
`list_pending_callbacks` returns it. Note what that last assertion does and does
not prove: it covers the callback path **for a row that has a
`callback_session_id`**. The no-callback case is a separate test against the
owed-notice drain, not this one. Plus idempotency; the
`_stronger_terminal_status` race; and must not reset `callback_status` from
`sent`/`skipped`.

**`tests/test_sqlite_sessions_store.py`** (PR5) —
`test_ensure_agent_session_id_adopts_row_owned_by_other_backend` (today raises
`IntegrityError`); `test_bind_agent_session_survives_concurrent_insert`;
`test_models_declare_scope_anchor_unique_index` (guards the drift).

**`tests/test_session_archive.py`** (PR5) — `:86 test_archive_reclaims_bound_resources`
is the exact template for `test_delete_agent_sessions_reclaims_bound_definitions`.

**`tests/test_agent_run_target.py`** (PR5) —
`test_resolve_agent_run_target_tolerates_concurrent_row_insert`.

**`tests/test_cli_task_command.py`** (PR6) — `test_task_show_reports_consecutive_failures`;
`test_task_list_brief_includes_last_error`;
`test_watch_health_ignores_the_supervisor_heartbeat_row` (a `watch_runtime` row
newer than the last real failure must not reset `consecutive_failures`);
`test_health_ignores_an_active_execution_newer_than_the_last_failure` (the
definition's own `running` row must not downgrade `failing`);
`test_health_window_counts_completed_failures_past_queued_rows` (`LIMIT N` worth
of `queued` rows must not displace them); `test_health_counts_runs_stored_with
_legacy_status_spellings` (a row stored `completed` must read as a success, not as
a missing one); `test_health_orders_by_completion_not_creation` (earlier-created
run finishing last owns the health state permanently, not transiently);
`test_canceling_a_retry_does_not_clear_a_failing_badge` and
`test_eviction_canceled_run_is_transparent_to_health` (the machine-reachable half
of the same rule); `test_n_cancellations_do_not_displace_a_failure_from_the_window`
and `test_interrupted_run_is_excluded_before_the_limit` (both assert the exclusion
is in the predicate, not the classifier — fill the window with rows that must not
count and check the older verdict still shows); `test_health_ages_out_after_the
_time_bound` (a single failure on a definition that stops firing must not read
`failing` forever). (`:1066` already covers `never_run`.)

**`tests/test_controller_idle_cleanup.py`** (PR3) — currently 43 lines,
config-only; add the `periodic_cleanup` ↔ pin-provider wiring.

**New, cross-cutting:** en/zh key-parity test over `vibe/i18n/{en,zh}.json`.

**UI:** `cd ui && npm run build` for PR6's badge strings and PR7's D6
"legacy — delivery only" marker.

**Scenario catalog.** This section has been wrong twice. The original draft said
no harness catalog existed and no scenario ID applied — true when the plan was
written, false by the time it was pushed. The 2026-07-27 replacement then audited
against a **truncated listing** and claimed 16 entries. The real count at
`35a5e13a` is **HFR-001 … HFR-040**, and `message_delivery` (`INDEX.yaml:46-52`)
is active as well. The audit below is against those 40. (By 2026-07-29 the
landed PRs extended the catalog to HFR-001…059 plus 240…279; the audit remains
valid for the 40 entries it covers, and each implementation PR re-audits.)

Because the earlier count was wrong, treat this table as the starting point for
each implementation PR's own re-audit, not as a substitute for it.

**Inverted — the entry states the behaviour D1 removes, so it is rewritten, not
extended:**

| ID | Current assertion | Effect |
| --- | --- | --- |
| **HFR-003** | "canceled scheduler execution **requeues** its claimed Run" (`kind: cancellation`, `test_drain_requests_requeues_cancelled_task_run`) | **PR2.** The name itself asserts the requeue. Entry and test both replaced. |

**Guardrails — must NOT move; they are the contracts that make the corrections
above correct, and a PR that changes them has misunderstood the plan:**

| ID | Assertion | Why it pins this plan |
| --- | --- | --- |
| **HFR-012** | "user-stopped Run settles **canceled**, not failed" | With HFR-037 and `SETTLEMENT_TERMINAL_STATUS`, this reserves `canceled` for user intent — which is why interruptions are `failed` (§3.3 correction). |
| **HFR-037** | "a stopped run settles canceled rather than succeeded" | Same axis, from the other side. |
| **HFR-029** | "backend runtime refresh settles its Run as a **refresh**, not a user stop" | The existing precedent that an infrastructure fault is not a cancellation. `evicted` / `restarted` follow it. |
| **HFR-014 / HFR-015 / HFR-017** | live, unknown, or merely-waiting Runs are never swept | PR2 changes what counts as "owned" by cancelling executions. Fail-closed must survive. |
| **HFR-040** | "no backend stop may use the terminal-turn default" | New settlement reasons must not widen the stop path. |

**Re-validate — behaviour these entries cover is touched by a staged PR:**

| ID | Assertion | Touched by |
| --- | --- | --- |
| **HFR-002** | accepted backend generation death releases owner and dispatches successor once | PR2 / D1 — "dispatches successor once" is the duplicate-execution axis. |
| **HFR-004** | restart recovers held run queues without flushing user-owned queue heads | PR7 — `recover_processing_runs` changes what "recovers" means. |
| **HFR-008** | cancel and restart remain idempotent | PR2 — post-D1 idempotency is stronger: neither path re-dispatches. Natural home for the duplicate-prompt regression test. |
| **HFR-009** | turn that ends without a terminal result settles its Run | PR7 — settlement moves to terminal time. |
| **HFR-013** | running Run whose owner vanished is swept terminal | PR2 — the sweep exempts `owned_run_ids`; cancelling the execution is what lets it reach the eviction case (see PR2's post-#1005 note). |
| **HFR-016** | queued Run stranded by a dead transport ages out with an explanation | PR2 correction 1 — terminalizing instead of requeueing changes which rows reach `queued` at all. |
| **HFR-018 / HFR-019** | leaked Session lock is released without freeing a live one; a finishing predecessor cannot steal a successor's lock owner | PR2 — `cancel_session_executions` mutates `_inflight_executions`, the exact predicate `_release_leaked_session_locks` tests. |
| **HFR-023** | terminal result cannot overwrite an acknowledged stop's settlement reason | PR2 / PR7 — `_stronger_terminal_status` must order the new `evicted` / `restarted` reasons. |
| **HFR-027** | gate-owned Workbench turn settles its own Run when no result arrives | PR7 — settlement timing. |
| **HFR-028** | swept Run's persisted queue segment is retired immediately | PR2 — the terminalize path must retire segments the requeue path did not. |
| **HFR-030** | terminal output that keeps the Run elsewhere is stamped turn-only | **PR1** — widening the trigger-kind gate changes which recorder the result lands in. |
| **HFR-031 / HFR-032** | turn lane / drain lane leaves an Activity-owned Run running | **PR1** — the fifth gate is `_activity_run_ids`; this is the contract that decides the include/exclude question §4 PR1 raises. |
| **MESSAGE-DELIVERY-001** | Scheduled result finalizes its delivery anchor | PR7 — anchor still finalizes, settlement moves. |
| **MESSAGE-DELIVERY-005** | One Run retains multiple outputs but callbacks only its terminal result once | PR1 — puts `scheduled`/`watch` rows on the ledger path this governs. |

**New entries owed to the catalog:** eviction-terminalizes (PR2),
service-stop-terminalizes (PR2 correction 1), interrupted-run-notification
(PR2 correction 2), restart-terminalizes-watch-runs (D1 correction),
**restart-terminalized run with no callback target still notifies** (PR7 restart
correction), **owed notice survives a crash between delivery and acknowledgement
without duplicating** and **a notify that delivers nothing does not acknowledge**
(PR6 acknowledgement protocol — the receipt-based ack plus stable-`failure_id`
dedup, the two entries that actually pin crash safety; the catalog should record
the guarantee as at-least-once, since the send-to-persist window stays open until
an outbox exists), **a gate-wait does not count as session activity** (PR3 item-2
correction — the immortal-session guardrail, stated as "no fake activity" rather
than "evict harder", since the paired entry is that a **long streaming turn must
survive** the ceiling per `core/services/dispatch.py:118-121`; both pair with the
existing HFR-014/015/017 eviction entries), **cancellation terminalizes every
claimed request type, `hook_send` and `webhook` included** (PR2 allowlist
correction — the entry exists because those two were the ones an enumeration
missed, so it must assert the rule rather than the three types someone remembered),
**an interruption notice is not suppressed by an in-progress failure streak** (PR6
lane separation — the entry D1 depends on, since the silence is only reachable for
a definition that was already failing), **one restart interrupting overlapping
`create_per_run` executions notifies for every run** (PR6 — asserts the *count*
equals the number of interrupted runs, because the weaker "a notice was sent"
assertion is what let a second suppression scope through review), **a user-stopped
run settles `canceled` with no interruption notice** and **ending an idle row is
silent and settles nothing** (PR2 End — the pair is the point: the first pins the
active path against HFR-012/029/037 as the other direction of one rule, the second
is what makes the settle safe to call *unconditionally* and so removes the state-read
race rather than narrowing it), and the D6 legacy marker (PR7).

Cross-platform verification via
the Incus regression environment — note the running local service **predates**
`agent_sessions.visibility` (commit `3857f832`), so anything validated against the
live DB shape must be re-validated post-migration.

## 7. Product decisions (resolved 2026-07-25)

**D1 (was Q1) — An interrupted run is FAILED, never silently re-dispatched, and
the user is told.** Applies to eviction, teardown, restart recovery, and lifetime
timeout. Rationale: a silent re-dispatch of a daily report posts twice, and a
re-run of a long turn from scratch is worse than a clean failure. The user
decides what happens next, so the notification must be **actionable**: what
failed, why, at what point, and how to re-run.

Implementation consequences:
- `recover_processing_runs` (`storage/background.py:1563-1587`) must
  **terminalize** `scheduled`/`agent_run`/`watch` rows with
  `error="interrupted: service restarted mid-turn"` instead of resetting them to
  `queued`, keeping the existing `watch_runtime` / deferred-terminal exclusions.
  **Correction (2026-07-27): this bullet used to exempt `watch` as "genuinely
  idempotent". It is not.** `core/watches.py::_enqueue_hook` (`:1301-1324`) calls
  `enqueue_hook_send(prompt=final_prompt, run_type="watch", ...)` — an arbitrary
  agent prompt, dispatched through the same agent-facing path as a scheduled run,
  with the same side effects (messages posted, files written). Requeueing one
  re-runs that turn. The genuinely non-agent case is the separate run type
  `watch_runtime`, the waiter-process heartbeat, which
  `recover_processing_runs` (`storage/background.py:2195-2220`) **already**
  excludes via `run_type != "watch_runtime"` — so the rule is *terminalize
  every agent-facing run type*, and no trigger-kind allowlist is needed at all.

  **Second exemption: the gate-parked follower (recorded here 2026-07-27 —
  §PR7 asked for it and this bullet did not have it).** "Exempt only
  `watch_runtime`" was wrong as a closed statement, and this is the one place a
  reader looks for D1's rule. §PR7 derives a second mandatory carve-out: a
  `running` harness row whose queued scheduled segment still exists is **parked,
  not mid-turn** — `running` there means "accepted, not yet started", so D1's
  duplicate-prompt premise does not apply and terminalizing it destroys work that
  was always going to run, preventing nothing. The rule is therefore *terminalize
  every agent-facing run type, exempting `watch_runtime`, deferred-terminal rows
  (already in the code at `:2207-2211`), **and** gate-parked followers* — the last
  derived per-run from the queued message rather than from a marker column or a
  session-level proxy. Owed tests live with PR7
  (`test_restart_resumes_a_gate_parked_follower_instead_of_failing_it`,
  `test_gate_queued_harness_follower_is_not_failed_by_the_orphan_sweep`).

  This gap is worth naming as a pattern, not just closing: §PR7 found the
  carve-out, wrote "D1's own statement in §7 needs the carve-out recorded
  alongside the `watch_runtime` exemption it already documents, or the next reader
  re-derives the bug from the rule" — and then the edit was never made. **A
  correction that names the section it must be copied into is not finished until it
  is copied there.** §7 is the section other PRs cite as the spec, and it is
  headed "resolved", so an incomplete rule here outranks a complete one in §4.
- PR2's eviction reconcile terminalizes rather than requeues, which **removes the
  retry-storm hazard** that the requeue design needed a ceiling for. The
  attempt-counter requirement in PR2 is therefore dropped; a simple
  `defer_run_terminal` → `settle_deferred_run` is enough.
- Every terminalized-by-interruption run must carry a distinguishable error class
  (e.g. `metadata.interrupt_reason ∈ {evicted, restarted, lifetime_timeout}`) so
  the notification can say which, and so PR6 can suppress the auto-pause counter
  for interruption-class failures (they are infrastructure faults, not a broken
  task definition).

**D2 (was Q2) — `/new` PAUSES bound scheduled definitions.** `enabled=0` +
`last_error` explaining that the bound session was cleared by `/new`, plus a
one-line notice in the `/new` reply naming how many tasks were paused and how to
resume. Not soft-delete: archive is terminal, `/new` is an everyday command.

**D3 (was Q3) — A `create_once` rebind PRESERVES the old workdir / agent / model.**
`_reserve_runtime_session` (`core/scheduled_tasks.py:3117`) re-resolves the scope
agent today, which could silently switch the backend under a running task. The
rebind path must carry the previous session's settings forward and only fall back
to scope defaults for values it cannot recover. This is only implementable if the
settings are captured *before* the session row is deleted — `run_definitions` has
no `model`/`reasoning_effort` column of its own — so D3 depends on the reclaim
snapshot in §PR5.1. The rebind still always notifies (§PR5.2).

**D4 (was Q4) — A cron job must never be blocked by its own previous run.**
Three parts:
1. **Fire becomes enqueue-only.** Drop the `await execution` in `_run_task`
   (`core/scheduled_tasks.py:2004-2005`) so the APScheduler job returns
   immediately. With `max_instances=1` (`:1946`) and a PR7-length turn, an
   awaited execution silently discards every subsequent fire
   ("maximum number of running instances reached") with no error surfaced.
2. **Per-run lifetime cap.** Reuse the existing `run_definitions.lifetime_timeout_seconds`
   column — currently watch-only (`core/watches.py:618-627`; `storage/background.py:1666`
   sets it `None` for tasks) — and honor it for scheduled runs. On expiry: cancel
   the execution, terminalize `failed` with `interrupt_reason=lifetime_timeout`,
   notify per D1. The cap is an **inactivity bound**, not a turn-duration
   timeout — observable turn progress re-arms it (see the PR7 watchdog
   correction): a hung run is cancelled, a healthy long turn is not.
3. **Default the cap below the cron period.** Because a pinned-session task
   serialises on `_inflight_sessions`, the next fire queues behind a hung
   predecessor; the cap is what actually unblocks it. Default to
   `min(configured_or_global_default, 0.8 × cron_interval)`, with the global
   default in `config/v2_config.py`.

Related: **the PR3 eviction pin is time-bounded** by the same principle — cap it
at the existing `stuck_active_floor_seconds` (1800s) so a permanently-stuck run
cannot create an immortal session. Past the cap: evict **and** reconcile.

**D5 (was Q5) — Failure notifications follow a delivery ladder, ending in DM.**
Order: (1) the definition's `deliver_key`; (2) the bound session's scope, if the
session is still alive; (3) the scope the definition was created from (caller
provenance); (4) **DM to the owner**.

Because a DM is context-free by construction, the notification body must carry
its own context: task name + id, **where the task was created (channel/thread,
with a deep link)**, when it last succeeded, the error and its class, the current
state (paused? next fire when?), and how to re-run or resume.

**Rung (5) is mandatory, not an implementation check (corrected 2026-07-27).** An
earlier revision made this an "implementation check before coding: confirm a
definition can always resolve an owner DM target… if so, add a final fallback."
The condition is already resolved on master, so the conditional is a way to ship
PR6 with the failure still silent — and PR6 is marked **approved**:

- A definition created from a plain CLI invocation has **no caller provenance at
  all**. `_definition_creation_metadata_from_caller` delegates to
  `_session_creation_metadata_from_caller`, whose first line is
  `if caller_context is None: return {}` (`vibe/cli.py:3973-3985`). No
  `created_by`, so rung (3) and rung (4) are both empty — there is no channel to
  fall back to and no user id to DM.
- An **unscoped `create_per_run`** definition can also have no delivery key, which
  empties rung (1); once its per-run session is missing or torn down, rung (2) goes
  with it.

So the ladder is not merely short in an edge case: for a caller-less definition
**every rung is empty**, and the two ways to reach that state are ordinary usage —
a human typing `vibe task add` at a terminal, and the session policy this document
already treats as a first-class case. A failure notice with nowhere to go is a
failure notice that is never written, which is D1 unmet for exactly the runs
nobody is watching.

**Requirement:** the workbench inbox row is **rung (5)** and always resolves,
because it is addressed to the workspace rather than to a person.

**"Widen the push helper" was not a mechanism (corrected 2026-07-29, review).**
The current inbox is per-session by construction: `persist_agent_message` only
derives an inbox row when a `session_id` is present,
`maybe_notify_inbox_message` (`core/web_push_notifications.py:71`) fires only
after that row exists and returns if the session id is absent, the inbox query
renders per-session cards (`storage/messages_service.py:list_inbox_sessions`),
and the frontend keys entries by `session_id`. Widening the push helper alone
therefore creates nothing — there is no row to notify about — and rung (5)
stays empty exactly as before.

The mechanism that makes rung (5) real, without teaching four layers a new
sessionless row shape, is a **synthetic workspace-notifications session**: a
well-known workspace-scoped session (stable reserved anchor, platform `avibe`,
hidden from ordinary session lists, exempt from `/new` clears and eviction — it
has no backend and no turns), created lazily the first time a sessionless
notice needs a home and reused thereafter. The notice writer resolves rung (5)
to that session id and persists an ordinary message row into it, at which point
inbox persistence, unread counts, realtime, Web Push, and acknowledgement all
work unchanged — the notice is a normal inbox card in a "workspace
notifications" session. This is in PR6's scope, not a follow-up, because
without it rung (5) is as empty as the four above it. Owed tests:
`test_a_caller_less_cli_definition_still_delivers_its_failure_notice`,
constructed by creating the definition with `caller_context=None` so the
emptiness is structural rather than mocked, and asserting the notice lands as a
readable inbox row in the workspace-notifications session — not merely that the
ladder was walked; and
`test_workspace_notification_session_is_created_once_and_reused`.

**D6 (was Q6) — Annotate historical rows; do not backfill.** ~77 `scheduled` and
67 `watch` rows carry `status=succeeded` with a 0.6s `completed_at` and empty
`result_text`. They are indistinguishable from honestly-settled rows once PR7
lands. Stamp them once with `metadata.pre_settlement_migration = true`, keyed on
that **premature-success signature** — `status=succeeded` + empty `result_text`
+ dispatch-time `completed_at` — not on run type and cutover time alone, so
honest historical failures and cancellations keep their record unannotated
(corrected 2026-07-29, review; a single UPDATE, no schema change, no data
loss) — and have the UI/CLI render a quiet "legacy — delivery only" marker.
Rejected: leaving them (silently misleading history) and backfilling
`result_text` from `messages` (expensive and incomplete). **Owner: PR7**
(assigned 2026-07-27 — it was unassigned until then, and PR7 is the change that
makes the ambiguity observable).

**D7 (OPEN — blocks PR7) — A crash-recovered `claimed` message row is FAILED,
not resumed.** *Proposed 2026-07-27; awaiting maintainer confirmation.*

When the process dies with a message row in `claimed`, recovery cannot tell
"the backend received it and was mid-turn" from "the backend never received it".
The durable record is identical in both cases, which is what makes this a product
decision rather than an implementation detail.

- **Resume** can duplicate agent side effects — posts, tool calls, spend.
- **Fail** can discard work the backend never received.

Proposed: **fail**, for two reasons. It follows **D1**, which already settles that
an interrupted run is `failed` and never silently re-dispatched; resuming would
carve an exception into D1 exactly where the evidence is weakest. And the error
costs are asymmetric in the direction that matters — a wrongly-failed turn
produces a visible, retryable notice, while a wrongly-resumed turn produces a
duplicate side effect that is both irreversible and invisible.

Note this is the **opposite** asymmetry from the owed-notice protocol, which
deliberately chose at-least-once delivery. That is consistent, not contradictory:
a duplicated *notification* is a papercut, a duplicated *agent turn* is an
irreversible external effect. Cheap-and-idempotent should retry;
expensive-and-side-effecting should not.

If the answer is **resume** instead, PR7 needs a de-duplication seam that does
not exist today, and that seam should be its own PR ahead of it.

## 8. Smaller findings worth fixing opportunistically

- `session_policy='none'` takes a **channel-wide** execution lock
  (`_execution_lock_key:2253` only special-cases `create_per_run`), serialising
  every threadless run in a channel against a throwaway session. Likely unintended.
- `vibe/cli.py:4313-4329` polls up to 1800s silently — a caller waiting 65
  minutes gets no signal the run was never claimed. A "queued for Ns" progress
  line would have surfaced the field incident in seconds.
- Agent-spawned children (background Bash, session-scoped cron/wakeups) die with
  the session (`process_isolation.py:22-56`, `claude_process_reaper.py:262-278`).
  Reconcile makes this **visible** but does not fix it — survivable background
  work is a separate plan item.
- Codex `evict_idle_transports` and the OpenCode shutdown are equally run-blind.
  **Decided (2026-07-27): all three, in PR2.** An earlier revision left this as
  "decide whether to ship Claude-first or all three at once", which reopened the
  question PR2 had just closed — PR2 requires every P3-matrix path to route
  through the shared teardown helper or carry an explicit staged PR number, and
  no such staged PR exists in the dependency graph. Choosing Claude-first would
  therefore have left Codex and OpenCode teardown run-blind after all seven PRs,
  preserving the wedged row and lock that PR2 exists to eliminate, with nothing
  in the plan tracking the remainder.

  If Codex or OpenCode turns out to need transport work too large for PR2, that
  is discovered during implementation and gets a numbered PR and a dependency
  edge at that point — not a standing option to ship two thirds of a fix. The
  rule this section kept violating: a deferral without a number is a deletion.
- Watches share `run_definitions` and the same `last_error`-overwrite plus
  no-notification behaviour — apply PR6 at the shared layer, not the task path only.

## 9. Constraints

- Never restart the local `vibe` service to validate.
- Tests hermetic; no writes to real `~/.avibe` state.
- User-visible strings through `vibe/i18n/` (backend) and
  `ui/src/i18n/{en,zh}.json` (frontend).
- `codex-expert` review before code on PR2, PR4, PR5, PR7.
- Cross-platform verification via the Incus regression environment.

## 10. What implementing PR1, PR5, and PR6 proved about this plan

This document went through 43 rounds of review before any code was written. None
of the corrections below were found by that review; every one was found by
running the code. Read this as evidence about **what document review cannot
check**, not as a list of errata: each item is a claim that was internally
consistent, well-cited, and wrong.

### 10.1 The widened trigger-kind set is two sets, not one (PR1)

§4 PR1 says to widen the gate to `{agent_run, scheduled, watch, webhook, hook}`
plus `activity_recovery`, and says `activity_recovery`'s ids "come from the fifth
gate rather than from `task_execution_id`". A literal reading gives **one** set,
and one set is wrong: three of the five sites read `task_execution_id` *as a run
id*, and `activity_recovery`'s is a synthetic `activity:<backend>:<id>`. Feeding
it to those sites addresses a write to a row that cannot exist —
`record_run_message` finds no row and returns silently, with no exception.

As implemented (#1063): `HARNESS_TRIGGER_KINDS` selects the recorder;
`HARNESS_RUN_ID_TRIGGER_KINDS` is the subset whose `task_execution_id` *is* a run
id, excluding `activity_recovery`. Its real ids ride on
`activity_completion_output`, which the delivered path already prefers.

### 10.2 "PR1 introduces no settlement transitions" is false

§4 justifies PR1's safety with "the status write is a no-op today". That holds
only for rows that are **already terminal**. A harness row still `running` when
its result is delivered now settles `succeeded` at terminal-result time — PR7's
semantic arriving early.

Reachability is narrow: async backends emit after `complete()`, so the row is
already terminal. A synchronous emit inside `handle_scheduled_message` reaches
the new path, but `complete()` overwrites status immediately after, so the final
row is identical. What changes is a **transient** terminal state: a crash inside
that window leaves the run settled rather than requeued. PR7 should inherit this
fact rather than the claim.

### 10.3 An emptiness guard on the text backfill is not sufficient (PR1)

§4 PR1 specifies a backfill "guarded so it only fills a NULL/empty value and
never overwrites text a real terminal transition wrote". That guard is necessary
and not sufficient: it says nothing about whether the delivered *outcome* agrees
with the stored one. Two contradictions followed, in opposite directions:

```
canceled('user stopped it')     + late SUCCESS -> result_text='daily report body'
succeeded                       + late FAILURE -> error='backend blew up'
failed('swept: owner vanished') + late SUCCESS -> result_text='the report, actually fine'
```

All three are user-visible, because `_build_callback_message` prefers
`result_text` over the fallback string that would otherwise explain the outcome.

The first fix enumerated unsafe pairs and missed one; the correct rule is
**equality** — backfill only when the stored terminal status equals the delivered
one. Equality is *total* where enumeration is not, so it cannot miss a pair. The
general principle, which PR2 and PR7 both need: **a genuine outcome disagreement
means the stored status is wrong, and settling it is PR7's job. No earlier PR may
paper over it by writing text that contradicts the status it leaves in place.**

### 10.4 Superseding a bound session row is not side-effect-free (PR5)

§4 PR5 treats the supersede as bookkeeping — "move its anchor aside … nothing is
deleted". Two consequences were unstated and both are load-bearing:

1. **Routing.** `resolve_session_id_target` derives the thread *solely* from
   `session_anchor` via `thread_id_from_session_anchor`, which reads
   `anchor.split(":", 1)[0]`. A bare `superseded:<id>` leaves that base as the
   literal `"superseded"`, matching no platform prefix, so every definition still
   pinned to the row silently delivers to the channel root. And because the row
   still exists, PR5's own unresolvable-binding recovery never fires.
2. **Deletion scope.** `delete_agent_sessions` matches
   `anchor == prefix OR anchor LIKE '<prefix>:%'` and **hard-deletes**; `/new`
   reaches it via `clear_session_base`. So the marker's *shape* decides whether a
   superseded row survives `/new`. Fixing (1) with a suffix reintroduced this as a
   regression until it was excluded explicitly.

The pair is the point: any future change to the marker format must be checked
against **both** a prefix-tolerant reader and a prefix-matching deleter. Neither
is discoverable from the supersede call site.

### 10.5 A paused watch cannot be recovered with task commands (PR5)

D2 pauses bound definitions on `/new` and the plan specifies a notice. Tasks and
watches are reclaimed by the same teardown but managed by two command groups:

```
$ vibe task resume <watch-id>
{"code": "task_not_found"}
```

A combined notice hands the watch half of its audience directions that cannot
reach the thing that was just paused — the same silent stop the notice exists to
prevent, one level down. The reclaim ledger already carries `definition_type`;
the notice must split on it. Both variants must also name the **re-pointing**
step, not just resume: a resume alone re-breaks against the cleared session.

### 10.6 §3 constraint 5 was stale

It states `core/scheduled_tasks.py` "imports no i18n at all". It does, and has a
`_t` helper. Upstream fixed this after the plan was written. Constraints derived
from the absence of something need re-checking before each PR, not once.

### 10.7 Scenario IDs must be allocated per PR, up front

PR1 and PR5 were implemented in parallel and **both claimed HFR-041…048** for
unrelated scenarios. Each PR's CI passed independently; only a manual comparison
caught it. They append to the same region of `catalog.yaml`, so git would have
raised a conflict rather than silently duplicating — but the semantic collision
was real and neither author saw it.

Reserved ranges for the remaining work, so this cannot recur. **Re-drawn
2026-07-29 against the merged catalog:** PR5 landed using HFR-240…279 — it blew
through its assigned overflow (240…249) and the merged catalog now owns the
whole block, which had been promised to PR6/PR2/PR3 — so every unlanded
overflow block moves above the highest landed ID. The rule that keeps this
stable: **an overflow block is dead the moment a landed catalog occupies any of
it; reassign from above the highest landed ID, never reuse or straddle.**

| PR | main block | overflow block |
|---|---|---|
| PR1 (#1063, merged) | HFR-041 … 048 (used) | — |
| PR5 (#1064, merged) | HFR-049 … 059 (used) | HFR-240 … 279 (used) |
| PR6 (#1072) | HFR-060 … 099 (full) | HFR-280 … 319 |
| PR2 | HFR-100 … 129 | HFR-320 … 349 |
| PR3 | HFR-130 … 154 | HFR-350 … 369 |
| PR4 | HFR-155 … 179 | HFR-370 … 389 |
| PR7 | HFR-180 … 219 | HFR-390 … 429 |

PR6's main block was widened three times — 15, then 20, then 25, now 40 —
because each review round added defects and their tests after the range was
drawn; PR5's landed 40-ID overflow is the same lesson from the other side.
(#1072 currently carries review-round tests that took no ID because its block
was full and its old overflow was occupied — the 280…319 block above is where
they belong.)

**Reserve generously — the blocks above are deliberately far
larger than any PR should need.** A range that has to grow is the cheap failure;
the expensive one is a PR quietly landing on a neighbour's block, which has now
happened twice (PR1/PR5 both claiming 041…048; PR5's overflow landing across
blocks promised to PR6/PR2/PR3) and is only visible if someone compares
branches by hand. The number of tests a PR needs is not known when its range is
drawn, because review adds most of them.

### 10.9 Three ways a fix can be silently inert (PR6)

Every item in §10 so far is a false claim about the code. These are different and
worse: **changes that are correct on the axis you check and inert on the one you
do not.** All three were found in PR6, all three passed their own tests, and none
logged anything.

1. **A partial index SQLite will not use.** `CREATE INDEX … WHERE json_extract(…)
   = 'pending'` builds successfully and is then ignored — SQLite's implication
   analysis does not match those terms against the query. Verified with
   `EXPLAIN QUERY PLAN` at 5,000 rows, with and without `ANALYZE`.

2. **An index expression that can never match, because SQLAlchemy bound it.** The
   composed `case()` / `func` form renders its literals as **bound parameters**,
   and a bound parameter cannot match an index expression. Results stay correct,
   the full scan stays, nothing is logged. The predicate must be literal SQL,
   shared by name between the migration and the query.

3. **A `LIMIT` that bounds nothing.** `ORDER BY` over an unindexed predicate
   produces `USE TEMP B-TREE FOR LAST TERM OF ORDER BY`, so the engine sorts the
   entire candidate set before returning the first row. The cost is not
   "proportional to history" — it is a full sort of it, every tick.

**The rule this yields:** a performance or correctness claim about a query is not
established until `EXPLAIN QUERY PLAN` is asserted in a test. PR6 pins both the
index name and the absence of `TEMP B-TREE`, so drift fails the suite instead of
quietly restoring the scan.

Note the shape these share with §10.2 and §10.3: in each case the reasoning was
sound on the axis examined — the index exists, the predicate is right, the limit
is present — and wrong on an axis nobody thought to examine. That is now three
occurrences in one PR, which is enough to treat it as the normal case rather than
bad luck.

### 10.10 The drain replays a live-path emitter — an unaddressed root (PR6)

**PR2 and PR7 both stamp owed notices and therefore inherit this. Read it before
starting either.**

Three review rounds on PR6 produced defects at a constant rate — three, three,
three — with no convergence. The cause is not review thoroughness. Rounds 2 and 3
were **the same defects at greater depth**: identity alignment failed twice,
boundedness failed twice.

The structural reason is that the drain **replays a live-path emitter**. Every
assumption `emit_backend_failure` bakes in for *reporting a failure now* is wrong
for *replaying one later*, and four separate symptoms of that one fact were
patched individually:

| Symptom | What the live assumption did to a replay |
|---|---|
| Turn settlement | finalized a live, unrelated turn |
| Auth recovery | turned a receipt into an interactive re-auth prompt |
| Identity (twice) | could not reproduce the key the live path had already used |

Four symptoms, four patches, **one root left standing**. Each patch is tested and
correct; nothing here says PR6 is unsafe. What it says is that the next symptom
of the same root has not been enumerated, and enumeration is the only defence
currently in place.

**The honest fix is a dedicated replay emitter** that shares rendering with the
live path and none of its lifecycle. That is a larger refactor than a review
round should absorb, and it touches outbound delivery for every backend — the
highest-blast-radius surface in the system — so it belongs in its own PR with its
own review, not appended to a 3,000-line one.

**Requirement for PR2 and PR7:** do not add a fifth caller to the live emitter
for a replay. If either needs to deliver a notice about a run that already
ended, use the replay emitter, and if it does not yet cover the case, extend it
rather than falling back.

### 10.11 A guard test can pin a proxy instead of the property (PR6)

The query-plan test added in round 2 asserted *"the index is named and there is no
temp sort"*. Both stayed true in round 3 while the drain remained unbounded: the
index covered the state term only, so SQLite reported it as used and still walked
every pending row to evaluate the backoff term.

The test pinned a **proxy** for boundedness rather than boundedness itself, and it
passed throughout. It now asserts the constrained terms, and a companion test
pins the migration's expressions against the query's — index/query drift is
silent by construction, since the planner simply declines to match while results
stay correct.

Two distinct causes of that drift were hit in a single round: literals rendered
as bound parameters, and editing one of two copies of the same expression.

**One trap this creates, worth stating because it nearly shipped:** making the
backoff a pure `<= now` range term means `NULL` never matches, so any notice
stamped without the field becomes **permanently unreachable** — a silent deletion
of exactly the notices this plan exists to deliver. `coalesce(..., '')` inside the
indexed expression keeps it a range term and keeps those rows visible (HFR-089).
A range predicate over a nullable column is a trap every time.

### 10.8 The transferable lesson

Four of the six defects above are the same shape: **a claim about what the
surrounding code does, stated confidently, cited accurately, and false.** Document
review checks a plan against itself. It cannot check a plan against a codebase.

The practical consequence for PR2, PR3, PR4, PR6 and PR7: treat every "X is
safe because Y does Z" in this document as a hypothesis with a test attached, and
write that test **first**. Both landed PRs found their real defect within minutes
of running a reproducing test, and neither found it while reading.
