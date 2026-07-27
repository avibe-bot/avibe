# Harness Run Reliability: Settlement, Reconcile, Delivery, and Visibility

Status: design approved; **none of PR1–PR7 implemented**. Re-verified against
`master` @ `35a5e13a` (2026-07-27): every defect P1–P6 below still reproduces.

Origin branch: `fix/harness-run-reconcile` (from `master` @ `5921ad39`, 2026-07-25).

**Line numbers throughout are relative to `5921ad39` and have drifted** —
`core/scheduled_tasks.py` alone took 17 commits in the following 30 days. Resolve
every reference by symbol, not by line.

### Relation to `agent-run-zombie-settlement.md` (#1005, landed 2026-07-26)

That plan is a **delta** cut from this one: two zombie classes specific to
`run_type='agent_run'`. It shipped the shared substrate these PRs build on —
`core/run_settlement.py` (settlement vocabulary), the guarded metadata-merging
writers, `sweep_stale_runs` (three staleness classes), and
`_release_leaked_session_locks`. Its own non-goals name this plan: *"turn-duration
timeout; PR7's scheduled/watch settlement change; PR2's teardown cancel; PR6's
notification ladder."*

Two consequences worth stating up front, both verified 2026-07-27:

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

### Per-problem landing status (verified 2026-07-27 against `35a5e13a`)

| Problem | PR | Status | Load-bearing evidence |
|---|---|---|---|
| P1 scheduled/watch settle at dispatch | PR7 | not landed | `TaskExecutionResult` still has no `complete_on_return`; only its sibling `AgentRunExecutionResult` does. The noop sink exists solely in `_execute_agent_run`. |
| P2 delivered runs record no `result_text` | PR1 | not landed | `git diff 5921ad39..35a5e13a -- core/message_dispatcher.py` shows no change to any `task_trigger_kind` gate. |
| P3 teardown never reconciles | PR2 | not landed | `grep -E "agent_runs\|request_store\|run_id" core/handlers/session_handler.py` → 0 matches. `cancel_session_executions` does not exist. |
| P4a eviction blind to queued work | PR3 | not landed | `evict_idle_sessions` still reads only the clock and in-memory maps, in both passes. |
| P4b drain loop unbounded | PR4 | not landed | `_drain_recovered_activity_outputs` is still the first inline `await` of every `_watch_store` tick; zero `wait_for` / `heartbeat` / `watchdog` in the file. |
| P5 pinned bindings break | PR5 | not landed | `storage/models.py` still declares the anchor index non-unique; `reclaim_bound_definitions` and `get_or_create_agent_session_row` do not exist. |
| P6 failures invisible | PR6 | not landed | `_task_last_status` byte-identical; no `emit_backend_failure` caller in `core/scheduled_tasks.py`. |

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

### P2 — Delivered runs never record `result_text`

The fork is `suppress_delivery`, **not** `post_to`:

- Suppressed path → `core/message_dispatcher.py:1520` → `:1563-1582` →
  `_record_suppressed_run_message` (`:954-975`), which has **no trigger-kind
  gate** → `record_run_message(terminal_status=None)` →
  `storage/background.py:1238-1245` writes only `result_text`/`message_ids`.
  That is the +80s back-fill.
- Delivered path → `core/message_dispatcher.py:1849
  _record_agent_run_terminal_result` → returns immediately at `:1085-1088`
  because `task_trigger_kind != "agent_run"`. Same gate in
  `core/message_output.py:37-42` and `message_dispatcher.py:1566,1576`.
  **Nothing is recorded, ever.**

`_build_context` sets `task_execution_id` for every trigger kind
(`core/scheduled_tasks.py:2833`) — the run id is available and deliberately
ignored.

Live confirmation: `b9596cbd1855` → session `sesm2ybv3fjww`, scope
`slack::channel::private-agent-run-…`, metadata `{"private_agent_run": true,
"no_delivery": true}` → suppressed → captured. `3848228cb675` → session
`sesjqmc6ntf44`, foreground Slack DM scope → delivered → empty.

**Same class, unreported:** all 67 live `run_type='watch'` rows have empty
`result_text`.

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
| Running-tab "End" | `running_agents.py:588-719` | No | No | Yes |
| Codex `evict_idle_transports` | `codex/agent.py:491-583` | No | Partial | Yes |
| **Restart sweep** | `storage/background.py:1563-1587` | **Yes — requeues, not terminalizes** | No | n/a |

**Enabling fact that makes the fix cheap:** callback dispatch is DB-polled, not
push. `list_pending_callbacks` (`storage/background.py:964-979`) needs only
`completed_at IS NOT NULL` + terminal status + `callback_status='pending'`, and
`_watch_store` ticks every 2s on `PRAGMA data_version`. **A reconcile that sets
`status` + `completed_at` gets the callback for free** — verified live on both
zombies.

### P4 — Delivery into an existing session has no liveness guarantee

Three verified facts:

**(a) Enqueue cannot refresh `last_activity`.** `session_last_activity` is a
process-local in-memory dict (`core/controller.py:198`). `vibe agent run` is a
**separate OS process** that only writes a SQLite row (`vibe/cli.py:4245-4265`).
The first touch happens deep inside the turn, at
`claude_agent.py:110 get_or_create_claude_session`.

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

### P5 — Pinned session bindings break permanently; the race that creates them

**Symptom 1 — dangling binding.** `resolve_session_id_target` raises at
`core/scheduled_tasks.py:254/259`; `_execute_task` catches (`:2513`), calls
`mark_task_result(error=…)` (`:2516`) and **leaves `enabled=1`** → fires and
fails forever.

Root cause is **`/new` hard-deleting sessions**, not GC and not eviction:

- Eviction touches no DB rows (verified for both Claude and Codex paths); there
  is no session pruner anywhere.
- `core/handlers/command_handlers.py:525 handle_new` → `clear_sessions(key)`
  (`:541`) → `storage/sessions_service.py:495 delete_agent_sessions` — deletes
  the **whole scope, no anchor filter** — and `clear_base(key, session_anchor)`
  (`:544`) → `WHERE anchor = prefix OR anchor LIKE 'prefix:%'` (`:513-517`).
- Definition `92ee5b68a938` is `create_once` with anchor
  `slack_D0BACLC37N3:definition_<hex>` (`vibe/cli.py:3999`). A `/new` in that DM
  computes prefix `slack_D0BACLC37N3` and the `LIKE 'prefix:%'` deletes the
  task's own session. Nothing updates `run_definitions.session_id`.
- **The asymmetry that makes it a bug:** the workbench archive path *does*
  reclaim — `storage/workbench_sessions_service.py:878 archive_session` vacates
  the anchor (`:914`) and soft-deletes bound definitions (`:924-929`, with a
  comment explaining exactly why). The IM `/new` path implements neither half.
  **Two teardown paths, one contract.**

`session_policy` semantics (`vibe/cli.py:3589`, `:3470`): `existing` (user-pinned),
`create_once` (reserve one at definition time), `create_per_run`, plus run-only
`create`/`fork`/`none`. At execution time `existing` and `create_once` are
identical — both just "a pinned `session_id`" — so neither self-heals.

**Symptom 2 — `UNIQUE constraint failed: (scope_id, session_anchor)`.** The
observed run (`d5385eb4fa28`) was a **deterministic anchor collision**, not
concurrency: at commit `2ca6d1bd` a `create_per_run` task minted
`session_anchor_for_target(target)` = `slack_U021CM6KLJE` on every fire.
Proof: the surviving row `sesc8behbvqvn` with that exact anchor was created at
`2026-06-25T09:56:00` — the previous day's fire of the same cron minute.
**That path is already fixed on master** by `7d55dc20` (#717): anchors now carry
`:runtime_<uuid12>` (`core/scheduled_tasks.py:2641-2646`), asserted by
`tests/test_scheduled_tasks.py:1488`.

Three **residual** exposures remain:
1. **Lookup key ≠ constraint key.** `_find_agent_session_row_id`
   (`storage/sessions_service.py:1128`) filters on `agent_backend` (`:1148`) and
   `status != 'archived'` (`:1144`); the unique index is `(scope_id,
   session_anchor)` only. A same-anchor row owned by another backend is
   invisible to the finder but visible to the index → INSERT explodes. Only the
   resume path guards this, with a comment naming the exact failure
   (`session_handler.py:1275`).
2. **IM inbound find-then-create race.** `core/services/agent_run_target.py:168-190`
   selects, then `:276` inserts with no `IntegrityError` catch. SQLite deferred
   transactions take no write lock at the SELECT.
3. **Schema drift.** The unique index exists only in the Alembic revision
   (`20260601_0011_session_anchor_unique.py:141-144`); `storage/models.py:107-138`
   declares only a non-unique index, and `SQLiteSessionsService.__init__` calls
   `metadata.create_all` (`:113`). **Any DB born from models-only — including
   tests — silently lacks the invariant**, which is why this was never caught.

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
   and `HarnessPage.tsx:1213-1216` renders `run.status` **raw and untranslated**.
   → **Do not add an `interrupted` status.** Express it as `canceled` + an
   `error` string.
2. **Use guarded writers only.** `settle_deferred_run` and `record_run_output`
   scope their UPDATEs to `queued|running` and resolve races via
   `_stronger_terminal_status` (`:110-118`). `update_run_status` (`:1042`) is
   **unguarded** — never use it for reconcile.
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

Seven PRs, ordered by risk-adjusted value. Each is independently shippable.

### PR1 — P2: capture `result_text` for every harness run (lowest risk, do first)

Widen the trigger-kind gate from `== "agent_run"` to the harness set
`{agent_run, scheduled, watch, webhook, hook}` at `core/message_dispatcher.py:1085`,
`:1566`, `:1576` and `core/message_output.py:37-42`.

Why it is safe: because `defer_run_terminal` refuses already-terminal rows and
`record_run_output`'s terminal UPDATE is scoped to `queued|running`, **the status
write is a no-op today** — only `result_text`/`message_ids`/`updated_at` land. No
schema, no UI, no status-timing change. It removes an arbitrary asymmetry
(suppressed runs already capture output), makes daily-report failures
diagnosable, and de-risks PR7 by proving the run-id plumbing for
`trigger_kind="scheduled"` in isolation.

Ship with: en/zh key-parity test; i18n the two hardcoded strings at
`core/scheduled_tasks.py:2210-2225`.

**Corrections from the 2026-07-27 re-verification — read before implementing:**

- **There are five gates, not four.** The fifth is `_activity_run_ids`
  (`modules/agents/claude_agent.py`), carrying the same
  `if spec.get("task_trigger_kind") != "agent_run": return []`. Widening only the
  four listed sites leaves Activity→run attribution unfixed, so background-task
  completions on harness runs stay unattributed.
- **Two of the sites must be edited as a pair.** The suppressed-branch gate routes
  to the rich recorder `_record_suppressed_agent_run_terminal_result`, and the
  `elif` immediately below it is the **negation** (`!= "agent_run"`) routing to the
  legacy `record_run_message`. Widening the first without rewriting the second
  silently changes which recorder harness results land in — a behaviour change
  beyond "also record `result_text`".
- **`activity_recovery` is a live trigger kind** (`core/scheduled_tasks.py`) absent
  from the proposed set. Include or exclude it deliberately; do not omit it by
  accident.
- The delivered-path early return is narrower than §2 P2 states: `run_ids` is first
  populated from `semantics.run_id` / `semantics.metadata["run_ids"]`, and only the
  *fallback* to `_coalesced_task_execution_ids` is gated. The consequence is
  unchanged, because the only producer of a non-empty `run_id` is
  `activity_completion_output`, whose ids come from the fifth gate above.

### PR2 — P3: reconcile on teardown

1. `ScheduledTaskService.cancel_session_executions(session_id) -> int` — scans
   `_inflight_executions` + `_session_lock_cache` and `task.cancel()`s. Called
   from `evict_idle_sessions` (`session_handler.py:1486-1495`) on **both**
   branches, wired through `core/controller.py`.
   This is the must-have: `except asyncio.CancelledError` (`:2437-2439`) requeues
   the run and `_on_execution_done` releases `_inflight_sessions` → **the wedge
   is gone**. Semantics match the restart sweep exactly, so no new vocabulary.
2. Per **D1**, the cancelled run is **terminalized, not requeued**:
   `defer_run_terminal` → `settle_deferred_run` with
   `metadata.interrupt_reason = "evicted"`. This also removes the
   eviction↔requeue storm hazard, so no attempt counter is needed. It does mean
   `_execute_claimed_request`'s `except asyncio.CancelledError` branch
   (`:2437-2440`) must distinguish an eviction cancel (terminalize) from a
   service-stop cancel (`stop()` at `:1862-1882`, which should still requeue).
3. A DB reconcile sweep for runs the cancel didn't reach, using guarded writers
   only. Order matters: **cancel first, then reconcile** — the happy case finds
   nothing to do. The callback comes free (§2 P3), which is what delivers D1's
   user-facing notification.
4. Promote the shared non-terminal-status constant (§3.3).
5. i18n the interrupted copy.

Follow-up in the same PR family: extend the hook to the other run-blind teardown
paths (Running-tab "End", controller shutdown, Codex/OpenCode) — per CLAUDE.md
§2 the reconcile belongs on a **shared teardown helper**, not stamped into
`evict_idle_sessions` alone.

**Post-#1005 note (2026-07-27).** Neither safety net that landed with #1005 can
substitute for this PR, and both fail for the same reason: `sweep_stale_runs`
exempts `owned_run_ids`, and `_release_leaked_session_locks` classifies a lock as
leaked only when `run_id not in self._inflight_executions`. An evicted-but-
uncancelled execution stays in `_inflight_executions` permanently, so it is
"owned" by both tests forever. Step 1 (cancel the execution task) is therefore not
just the cheapest half of this PR — it is what makes the already-landed sweep
reach this case at all. Extend `core/run_settlement.py` with `evicted`, which
`:14` already reserves for this plan.

### PR3 — P4a: eviction interlock + activity touch at claim

1. **Pin provider:** `pinned_composite_keys()` built from
   `request_store.list_pending()` + `_inflight_executions`, consumed by
   `evict_idle_sessions` (`session_handler.py:1441-1454`). Must be recomputed
   **inside the second recheck pass** or the existing two-pass structure
   reintroduces the hole. Must **fail open** (unresolvable `session_id` does not
   pin) or a dangling P5 binding creates an immortal session. Reuses PR2's
   resolver (§3.4). Per **D4**, the pin is **time-bounded** at
   `stuck_active_floor_seconds` (1800s); past that, evict **and** reconcile.
2. **Touch at claim:** `_spawn_execution` (`:2034`) →
   `touch_session_activity(composite_key)`; also after `get_session_info`
   (`message_handler.py:169`) and on a timer while blocked on `gate.lock`
   (`modules/agents/service.py:172`). Idempotent; no-ops for unknown keys.
   Note this **cannot** be done at enqueue — different process, in-memory dict.

Neither changes what a Run means, so PR3 does not collide with PR7.

### PR4 — P4b: unstall and supervise the drain loop (own review)

This is the actual root cause of the 65-minute outage and the reason a restart
was needed. **PR3 does not prevent a recurrence** — it only stops the outage from
also destroying the target session.

- Move `_drain_recovered_activity_outputs` / `_drain_callbacks` /
  `_drain_vault_callbacks` off `_watch_store`'s critical path (own tasks, or
  `asyncio.wait_for`-bounded); timeout `emit_agent_message` at `:1753` and
  **requeue rather than drop** (`registry.requeue_completed_output` at `:1781`).
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
a hang there stalls every other tenant of `_watch_store`. If that distinction does
not survive review, PR4 reduces to its two uncontested halves — the per-tick
heartbeat/watchdog and the `_drain_dirty` re-arm — which are worth shipping alone
because they make the next occurrence self-diagnosing.

Re-verification note: the drain block *does* now re-arm on exception
(`except Exception: self._drain_dirty = True; raise`), but the `_drain_requests`
skip branches still do not — the capacity break and the
transport-unavailable / session-busy branches only `record_skip_reason` and
`continue`. That half of the bullet above is still open.

### PR5 — P5: stop orphaning sessions, harden reservation

1. **Reclaim on delete (fixes the cause).** Extract the archive reclaim body
   (`workbench_sessions_service.py:922-935`) into a shared
   `reclaim_bound_definitions(conn, session_id)` and call it from
   `delete_agent_session`/`delete_agent_sessions` (`storage/sessions_service.py:475/495`).
   Per **D2**, the `/new` path **pauses** (`enabled=0` + `last_error` naming
   `/new` as the cause) rather than soft-deleting, and `/new`'s reply gains a
   one-line notice with the count and how to resume.
2. **Self-heal `create_once` only.** In `_execute_task` (`:2482`), catch the
   unresolvable-session `ValueError`; if `session_policy == "create_once"` and
   `metadata.session_scope_id`/`deliver_key` survive, re-reserve via
   `_reserve_runtime_session`, persist through `store.update_task(session_id=…)`,
   continue — **and always notify** (a silent rebind that loses continuity is a
   worse bug than the failure). Per **D3** the rebind **carries the previous
   session's workdir / agent / model forward**, falling back to scope defaults
   only for values it cannot recover — `_reserve_runtime_session` re-resolves the
   scope agent today and would otherwise switch the backend silently. For
   `existing`, never rebind: pause + notify.
3. **Auto-pause backstop** for the unresolvable-target error class only.
4. **Keyed get-or-create** `get_or_create_agent_session_row(conn, scope_id,
   session_anchor, …)` in `storage/agent_session_rows.py`: look up by the
   *constraint* key, insert, catch `IntegrityError` under `conn.begin_nested()`
   and re-read. Route `ensure_agent_session_id`, `bind_agent_session` and
   `agent_run_target.py:276` through it; adopt foreign-backend rows using the
   resume path's existing decision (`session_handler.py:1277-1285`) so there is
   one policy, not two.
5. **Add the unique index to `storage/models.py`** so `create_all` and Alembic
   agree — do this regardless; it is the difference between tests reproducing
   production and not.
6. Integrity check in `reconcile_jobs` (`:1917`) flagging definitions whose
   `session_id` no longer resolves at scheduler startup.

### PR6 — P6: make failure visible

1. **Notify once per failure transition**, at the single choke point
   `_execute_task:2513-2517` (same site as PR5's pause — implement one path:
   classify → maybe rebind → maybe pause → notify once). Reuse
   `core/backend_failure.py:emit_backend_failure`, whose `metadata.event =
   "backend_failure"` is already honored by `web_push_notifications._is_notifiable_message`.
   For a dead session, build the context from `deliver_key`/`metadata.session_scope_id`
   via `_resolve_delivery_target` + `_build_context` (`:2763`) — that is the one
   piece of new plumbing. Delivery follows **D5**'s ladder, ending in a DM whose
   body carries its own context (task name/id, creating channel/thread with a deep
   link, last success, error class, current state, how to resume). Verify the
   owner-DM fallback can always resolve; if not, widen the workbench inbox shape.
   The same notification serves **D1** for interrupted runs, with
   `metadata.interrupt_reason` selecting the copy.
2. **Derived health, no migration:** add `consecutive_failures` / `recent_failures`
   to `_task_payload` (`vibe/cli.py:1505`) and the harness API
   (`vibe/ui_server.py:8083`) via one indexed query over
   `agent_runs WHERE definition_id = ? ORDER BY created_at DESC LIMIT N`
   (`ix_agent_runs_definition_created` exists; batch with a window function for
   the list endpoint). A schema-based counter on `run_definitions` is the
   follow-up if `agent_runs` retention ever becomes lossy (Q6).
3. **Fix the reporting bug:** `_task_last_status` must not report `succeeded`
   while unacknowledged failures exist; include `last_error` in the `brief`
   payload; add a health badge to Harness list rows, not just the detail pane.
4. Policy: notify on the 1st failure (once, not daily); auto-pause at 3
   consecutive failures **only** for the unresolvable-target class — a transient
   agent error must not disable a task.

### PR7 — P1: settle scheduled/watch runs at the real terminal result

The end state the docs already claim as deferred. Changes:

- `TaskExecutionResult` gains `complete_on_return` (`:211-215`); honored at `:2446`.
- `_execute_task`/`_execute_request` route through
  `dispatch_turn(..., on_chunk=noop)` mirroring `:2598-2609`.
- `mark_task_result` (`:2516`) moves to terminal time — **otherwise
  `vibe task list` keeps reporting `succeeded` at dispatch even after the run row
  is honest.**

No schema change, no new status value, no UI/i18n work; historical rows keep
their (wrong) values and only new rows get honest timing.

**Two safety mitigations ship in the same PR — without them PR7 is a regression:**

- **Restart must not re-dispatch (D1).** Otherwise `recover_processing_runs`
  becomes a duplicate-prompt generator: a mid-flight daily report re-sent after
  restart posts twice. Recovered `scheduled` rows terminalize with
  `interrupt_reason=restarted` (mirroring the `watch_runtime` exclusion at `:1570`).
- **The cron must not be blockable (D4).** Today `_run_task` awaits the execution
  (`:2004-2005`) under `max_instances=1` (`:1946`); with a PR7-length turn a hung
  run silently discards every subsequent fire. Fire becomes enqueue-only, plus a
  per-run lifetime cap honoring `run_definitions.lifetime_timeout_seconds`
  (currently watch-only) defaulted to `min(configured, 0.8 × cron_interval)`.
  There is **no turn-duration timeout anywhere by design**
  (`core/services/dispatch.py:116-118`), so this cap is the only backstop.

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
PR1 (P2 capture)          — independent, ship first
PR2 (P3 reconcile)        — provides the session→runs resolver
  └─ PR3 (P4 interlock)   — reuses that resolver
       └─ PR4 (P4 drain)  — own review, own PR
PR5 (P5 bindings)         — independent; shares the notify hook with PR6
  └─ PR6 (P6 visibility)  — same choke point as PR5's pause
PR7 (P1 settlement)       — needs PR1 landed and PR6's notify path available
                            (D1 requires an actionable failure notification)
```

All six product decisions are resolved (§7); nothing is blocked on further input.

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
- PR2: cancelling an in-flight execution requeues the run **and** discards its
  `_inflight_sessions` lock so the next drain dispatches — the regression test
  for the wedge, highest-value new case. Plus retry-counter → terminal at ceiling.
- PR3: `test_spawn_execution_touches_target_session_last_activity`;
  `test_pending_agent_run_pins_target_session_against_idle_eviction`;
  `test_unresolvable_session_id_does_not_pin_a_session`.
- PR4: `test_drain_rearms_when_a_pending_request_is_skipped`;
  `test_watch_store_tick_is_not_blocked_by_a_hung_recovered_activity_delivery`
  — the direct regression test for the field incident.
- PR5: `test_execute_task_pauses_and_notifies_when_pinned_session_is_missing`;
  `test_create_once_rebinds_when_session_deleted`;
  `test_existing_policy_never_rebinds`; `test_repeated_failures_do_not_notify_twice`.
- PR7: clone `test_agent_run_stays_running_until_terminal_result` (`:2054`) as
  `test_scheduled_run_stays_running_until_terminal_result`. **`:4098`
  `test_drain_requests_records_scheduled_create_per_run_reserved_session`
  asserts `payload["ok"] is True` immediately after drain (`:4148-4151`) — it
  locks in the bug and must be updated.** Same shape at `:4039` for watch.
  Plus a restart test asserting a mid-flight scheduled run is not requeued into
  a duplicate prompt.

**`tests/test_claude_cli_path.py`** (PR2/PR3) — all 10 existing
`evict_idle_sessions` tests live here (`:1323-2130`), with a stub `_Controller`
(`:68-92`) and frozen `time.monotonic`: eviction with an in-flight run calls
`cancel_session_executions`; without one, no spurious DB hit; bounded exemption
below/at the ceiling.

**New `tests/test_session_idle_eviction_interlock.py`** — pinned/unpinned, pin
appearing *between* the two passes, provider raising (must fail open).

**`tests/test_inbox_events.py`** (PR2) — reconcile on a `running` row →
`canceled` + `completed_at` + `callback_status` still `pending`, then
`list_pending_callbacks` returns it (proves the free-callback path); idempotency;
`_stronger_terminal_status` race; must not reset `callback_status` from
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
`test_task_list_brief_includes_last_error`. (`:1066` already covers `never_run`.)

**`tests/test_controller_idle_cleanup.py`** (PR3) — currently 43 lines,
config-only; add the `periodic_cleanup` ↔ pin-provider wiring.

**New, cross-cutting:** en/zh key-parity test over `vibe/i18n/{en,zh}.json`.

**UI:** `cd ui && npm run build` for PR6's badge strings.

**Scenario catalog:** none exists for harness runs (`tests/scenarios/auth_setup/`
is the only catalog), so no scenario ID applies. Cross-platform verification via
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
  **terminalize** `scheduled`/`agent_run` rows with
  `error="interrupted: service restarted mid-turn"` instead of resetting them to
  `queued`. Keep the requeue path only where a trigger is genuinely idempotent
  (`watch`), and keep the existing `watch_runtime` / deferred-terminal exclusions.
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
`_reserve_runtime_session` (`:2645`) re-resolves the scope agent today, which
could silently switch the backend under a running task. The rebind path must
carry the previous session's settings forward and only fall back to scope
defaults for values it cannot recover. The rebind still always notifies (§PR5.2).

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
   notify per D1.
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

**Implementation check before coding:** confirm a definition can always resolve an
owner DM target. A task created purely from the CLI may have no user id in its
provenance, which makes rung (4) empty. If so, add a final fallback to a
workbench inbox row — noting that `maybe_notify_inbox_message`
(`core/web_push_notifications.py:71`) currently requires `messages.session_id`,
so that shape needs widening.

**D6 (was Q6) — Annotate historical rows; do not backfill.** ~77 `scheduled` and
67 `watch` rows carry `status=succeeded` with a 0.6s `completed_at` and empty
`result_text`. They are indistinguishable from honestly-settled rows once PR7
lands. Stamp them once with `metadata.pre_settlement_migration = true` (a single
UPDATE, no schema change, no data loss) and have the UI/CLI render a quiet
"legacy — delivery only" marker. Rejected: leaving them (silently misleading
history) and backfilling `result_text` from `messages` (expensive and
incomplete).

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
- Codex `evict_idle_transports` and the OpenCode shutdown are equally run-blind;
  decide whether to ship Claude-first or all three at once.
- Watches share `run_definitions` and the same `last_error`-overwrite plus
  no-notification behaviour — apply PR6 at the shared layer, not the task path only.

## 9. Constraints

- Never restart the local `vibe` service to validate.
- Tests hermetic; no writes to real `~/.avibe` state.
- User-visible strings through `vibe/i18n/` (backend) and
  `ui/src/i18n/{en,zh}.json` (frontend).
- `codex-expert` review before code on PR2, PR4, PR5, PR7.
- Cross-platform verification via the Incus regression environment.
