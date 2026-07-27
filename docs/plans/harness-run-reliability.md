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
   → **Do not add an `interrupted` status.** Express it as **`failed`** + an
   `error` string + `metadata.interrupt_reason`.
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

Seven PRs, ordered by risk-adjusted value. Each is independently shippable
**except PR2**, which the 2026-07-27 re-verification found to need PR6's
notification path — see §5 and PR2 correction 2.

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
   `_inflight_executions` + `_session_lock_cache` (plus the new `run_id →
   session_id` map below, which is what makes `create_per_run` visible) and
   `task.cancel()`s. Called
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
   `metadata.interrupt_reason = "evicted"`. This also removes the
   eviction↔requeue storm hazard, so no attempt counter is needed. Concretely,
   `_execute_claimed_request`'s `except asyncio.CancelledError` branch
   (`core/scheduled_tasks.py:2821-2822`) stops requeueing and terminalizes
   **every agent-facing run type** — `scheduled`, `agent_run`, `watch` — with only
   `watch_runtime` exempt. *(Corrected 2026-07-27: this used to say "branch on the
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

> **Terminalize every agent-facing run type** — `scheduled`, `agent_run`,
> `watch` — with `metadata.interrupt_reason ∈ {evicted, restarted}`. The only
> exempt run type is `watch_runtime` (the waiter heartbeat, not a turn), and
> `recover_processing_runs` already excludes it at
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
`metadata.owed_interruption_notice`, written by the same guarded UPDATE that
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

### PR3 — P4a: eviction interlock + activity touch at claim

1. **Pin provider:** `pinned_composite_keys()` built from
   `request_store.list_pending()` + `_inflight_executions`, consumed by
   `evict_idle_sessions` (`session_handler.py:1441-1454`). Must be recomputed
   **inside the second recheck pass** or the existing two-pass structure
   reintroduces the hole. Must **fail open** (unresolvable `session_id` does not
   pin) or a dangling P5 binding creates an immortal session. Reuses PR2's
   resolver (§3.4). Per **D4**, the pin is **time-bounded** at
   `stuck_active_floor_seconds` (1800s); past that, evict **and** reconcile.
2. **Touch at claim:** `_spawn_execution` (`core/scheduled_tasks.py:2705`) →
   `touch_session_activity(composite_key)`; also after `get_session_info`
   (`message_handler.py:169`). Idempotent; no-ops for unknown keys.
   Note this **cannot** be done at enqueue — different process, in-memory dict.

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

   **Snapshot the session's settings before the row is deleted (added
   2026-07-27).** D3 requires the later rebind to carry the previous session's
   agent / model forward, and as written that is not achievable: `run_definitions`
   (`storage/models.py:173-197`) stores `agent_name` and `cwd` but **no** `model`
   and **no** `reasoning_effort`, while `agent_sessions` carries both
   (`storage/models.py:116-117`). The session row is hard-deleted, so by the time
   `_execute_task` rebinds there is nowhere left to read the old model from and
   `_reserve_runtime_session` (`core/scheduled_tasks.py:3117`) resolves the
   *current* Agent row instead — a silent settings change, which is exactly what
   D3 forbids. So `reclaim_bound_definitions` must, **in the same transaction and
   before the delete**, copy the resolved `model` / `reasoning_effort` (and any
   other settings that live only on the session row) into durable definition
   metadata. Reclaim is the only code path that still sees both rows; anywhere
   later is too late. Persist it as explicit metadata keys rather than new
   columns, so the same snapshot serves the archive path already calling this
   body (`workbench_sessions_service.py:922-935`).
2. **Self-heal `create_once` only.** In `_execute_task` (`:2482`), catch the
   unresolvable-session `ValueError`; if `session_policy == "create_once"` and
   `metadata.session_scope_id`/`deliver_key` survive, re-reserve via
   `_reserve_runtime_session`, persist through `store.update_task(session_id=…)`,
   continue — **and always notify** (a silent rebind that loses continuity is a
   worse bug than the failure). Per **D3** the rebind **carries the previous
   session's workdir / agent / model forward**, falling back to scope defaults
   only for values it cannot recover — `_reserve_runtime_session` re-resolves the
   scope agent today and would otherwise switch the backend silently. Its source
   is the snapshot item 1 writes before the delete, not the (now absent) session
   row; where the snapshot is missing — a definition orphaned before this lands —
   the rebind falls back to scope defaults *and says so in the notice*, so the
   user can tell a preserved rebind from a reset one. For `existing`, never
   rebind: pause + notify.
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
5. **The owed-interruption-notice drain (assigned 2026-07-27).** Not just the
   copy — the whole delivery mechanism: scan `metadata.owed_interruption_notice ==
   "pending"` on the existing 2 s tick, deliver through (1) above, and acknowledge
   under the protocol specified in PR7's restart correction (structured
   `(delivered_id, persisted_row, error)` result, ack on either evidence of
   delivery, `attempts`/`next_attempt_at` backoff, `failed` dead letter, and the
   `persist_agent_message` error channel). **This must land with PR6 and not with
   PR7**, because PR2 depends on PR6 and starts writing `pending` notices — see
   the §5 ownership correction. It is the larger half of PR6 by implementation
   weight, so size the review accordingly; if PR6 has to be split, the drain is the
   half PR2 blocks on.

### PR7 — P1: settle scheduled/watch runs at the real terminal result

The end state the docs already claim as deferred. Changes:

- `TaskExecutionResult` gains `complete_on_return` (`:211-215`); honored at `:2446`.
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

The seam already exists and PR7 should extend it rather than invent one.
`settle_agent_runs_without_result` (`core/scheduled_tasks.py:3038`) is the **turn
lane**'s settler: `SessionTurnManager` calls it (`core/session_turns.py:827`) when
an avibe turn ends without a terminal result, using the same guarded writers and
i18n as the drain lane. Today it only handles the *absence* of a result. PR7 needs
the positive case on the same lane: when a gated turn ends **with** a terminal
result, settle the run from there, at terminal time, with the result attached.
Concretely:

- the gate lane carries the `execution_id` through to turn end (it is already in
  `context.platform_specific`, `_build_context:3335`), so the turn-end hook can
  name the run it belongs to;
- terminal settlement goes through the same `settle_deferred_run` /
  `record_run_output` writers both lanes already share, so `_stronger_terminal_status`
  keeps arbitrating between the two lanes rather than letting the later writer win;
- `mark_task_result` moves to terminal time on **both** lanes, not just the
  direct-dispatch one, or `vibe task list` stays dishonest for precisely the
  Workbench runs this PR is about.

Regression coverage must be avibe-targeted, not just IM-targeted:
`test_scheduled_avibe_run_settles_at_terminal_result_not_at_gate_submit` — submit
a scheduled run at an avibe session, assert the run stays `running` across
`submit_scheduled` returning, and only reaches a terminal status when the gated
turn produces its result. The IM-lane test alone would pass against the broken
gate path, which is how this gap survived into the plan.

**Historical rows are PR7's job too (D6, assigned 2026-07-27).** This section
previously said historical rows "keep their (wrong) values" and claimed no UI/i18n
work, which left D6 owned by nobody — PR1–PR6 do not touch it either. That is not
a deferral, it is a regression PR7 itself creates: the ~144 legacy rows are
distinguishable from honest ones *today* only because **every** row is dishonest.
The moment PR7 lands, they become indistinguishable, and history quietly lies. So
D6 ships here:

- one-shot `UPDATE` stamping `metadata.pre_settlement_migration = true` on the
  `scheduled`/`watch` rows that predate the cutover (no schema change);
- a quiet "legacy — delivery only" marker in the CLI run views and the UI run
  detail, reading that flag — which does mean PR7 carries **one** i18n string
  pair (`vibe/i18n/` + `ui/src/i18n/{en,zh}.json`).

If PR7 gets split for review size, the stamp and the marker must land in the
**same** release as the settlement change, not a follow-up.

**Two safety mitigations ship in the same PR — without them PR7 is a regression:**

*(Scope note, 2026-07-27: the acknowledgement protocol derived under the first
mitigation below is **implemented by PR6**, not here — PR7 only stamps
`owed_interruption_notice=pending` and relies on PR6's drain, which by then
already exists. §5 ownership correction has the reasoning.)*

- **Restart must not re-dispatch (D1).** Otherwise `recover_processing_runs`
  becomes a duplicate-prompt generator: a mid-flight daily report re-sent after
  restart posts twice. Recovered `scheduled`, `watch` **and** `agent_run` rows
  terminalize with `interrupt_reason=restarted`; `watch_runtime` stays exempt via
  the `run_type != "watch_runtime"` filter already at
  `storage/background.py:2202`. (`watch` is included deliberately — see the D1
  correction in §7: a `watch` run carries an arbitrary agent prompt.)

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
  - **(b) Persist an owed notice.** Stamp `metadata.owed_interruption_notice`
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

  1. **State, not boolean.** `owed_interruption_notice` is
     `pending` → `sent`, mirroring the `callback_status` vocabulary already in use
     (`pending`/`sent`/`skipped`/`failed`, written through
     `update_callback_status` at `core/scheduled_tasks.py:1272`). Same shape, same
     drain, nothing new to learn. `skipped` covers a row the renderer decides
     needs no user-visible notice; `failed` covers a delivery that errored, and
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
     are safe to attempt because `agent_message_exists`
     (`core/message_dispatcher.py:1547`) already guards the send path against
     re-posting an identity that did persist.
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
PR5 (P5 bindings)         — independent; shares the notify hook with PR6
  └─ PR6 (P6 visibility)  — same choke point as PR5's pause; OWNS THE WHOLE
       │                    owed-notice drain: renders it AND implements the
       │                    receipt/ack/backoff/dead-letter protocol plus the
       │                    (delivered_id, persisted_row, error) result and the
       │                    persist_agent_message error channel it needs
       └─ PR2 (P3 reconcile) — stamps metadata.owed_interruption_notice
       │                       (correction 2: a terminalized run with no
       │                       callback_session_id is otherwise silent,
       │                       violating D1); provides the session→runs resolver
       └─ PR3 (P4 interlock) — reuses that resolver
            └─ PR4 (P4 drain) — own review, own PR
PR7 (P1 settlement)       — needs PR1 landed and PR6's drain available; only
                            STAMPS the owed notice from the restart path, which
                            never reaches _execute_task at all (see PR7's
                            restart correction); also carries D6's history
                            stamp + legacy marker
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
both write `metadata.owed_interruption_notice` inside the guarded UPDATE that
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
documented there as the rejected alternative. No implementation choices remain
open.

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
- PR2: cancelling an in-flight execution **terminalizes** the run (D1, not the
  pre-D1 requeue this line used to describe) **and** discards its
  `_inflight_sessions` lock so the next drain dispatches — the regression test for
  the wedge, highest-value new case. There is no retry-counter case: D1 dropped
  the attempt ceiling along with the requeue. Add two more:
  `test_service_stop_terminalizes_a_scheduled_run_instead_of_requeueing` (PR2
  correction 1 — the duplicate-prompt regression) and
  `test_evicted_scheduled_run_without_a_callback_target_still_notifies`
  (correction 2 — guards against the silent terminal row).
- PR3: `test_spawn_execution_touches_target_session_last_activity`;
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
  locks in the bug and must be updated.** Same shape at `:4039` for watch.
  Plus a restart test asserting a mid-flight scheduled run is not requeued into
  a duplicate prompt.
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
appearing *between* the two passes, provider raising (must fail open).

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
`test_task_list_brief_includes_last_error`. (`:1066` already covers `never_run`.)

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
is active as well. The audit below is against all 40.

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
existing HFR-014/015/017 eviction entries), and the D6 legacy marker (PR7).

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
  excludes via `run_type != "watch_runtime"` — so the correct rule is *terminalize
  every agent-facing run type, exempt only `watch_runtime`*, and the exemption is
  already in the code. No trigger-kind allowlist is needed at all.
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
incomplete). **Owner: PR7** (assigned 2026-07-27 — it was unassigned until then,
and PR7 is the change that makes the ambiguity observable).

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
