# Unified Background-Work Banner (P1) — spec

Owner decision (2026-07-16 20:51): the workbench chat's background-task banner must present ONE
unified concept of background work for a session, regardless of where the work runs. Harness
items (watches, scheduled tasks, delegated agent runs) join the same banner that today only
shows backend-reported Activities (e.g. Claude background tasks).

**P2 explicitly shelved (owner, same message):** an Avibe-owned cross-backend background-execution
tool (MCP-delivered) is NOT being built now — revisit when Avibe develops its own Agent. Do not
implement any execution-layer changes under this spec; this is presentation/projection only.

## Model (the one rule)

The banner's source of truth is a **union assembled at runtime-state build time**:

1. **Backend activities** — the existing process-local `SessionActivityRegistry`
   (`core/session_activities.py`, #864). Unchanged.
2. **Harness items** — derived LIVE from the durable store at assembly time, never duplicated
   into the registry (a watch survives restarts; the registry does not — deriving from the DB
   keeps the banner correct-by-construction after a restart):
   - enabled watches whose callback session is this session;
   - pending/scheduled tasks targeting this session;
   - running/queued delegated agent runs whose callback returns to this session (work this
     session dispatched and is waiting on).

Each unified item carries: `kind` (`backend_activity` | `watch` | `task` | `agent_run`),
`label` (watch name / task summary / target agent + short message head / activity description),
`since` timestamp, and a stable id. Task items also carry the durable `schedule_type` (`at` |
`cron`) used for display classification. The banner count = union size. No controls
(pause/cancel) in this phase — the expanded list links to the Harness page (watches/tasks) or
run detail.

## UX

- Banner line: `后台任务 · N` (`chat.activities.running`).
- Expanded list: one row per item — kind icon + label + relative time. Rows for harness items
  navigate to their Harness surface on click; backend-activity rows keep current behavior.
- Chinese row kinds use `监听` for watches, `一次性` for `schedule_type=at`, and `周期性` for
  `schedule_type=cron`. Classification must use `schedule_type`, never parse the task label.
- Empty union → banner hidden (current behavior).
- All new strings through i18n (en + zh).

## Code anchors

- Runtime-state assembly: wherever `background_activities` is built for the session runtime
  state payload (search `background_activities` producers in core/vibe; ChatPage consumes
  `runtimeState.background_activities` at ui/src/components/workbench/ChatPage.tsx ~1632).
- Registry: `core/session_activities.py` (`SessionActivity`, `SessionActivityRegistry`).
- Harness state: `run_definitions` (watches), scheduled tasks store, `agent_runs`
  (status running/queued, callback lineage) — read-only queries.
- Banner UI: ChatPage banner block (~1626-1670) + `chat.activities.*` i18n keys.

## Owner design review (2026-07-16 21:15) — five requirements folded in

1. **Popover opens UPWARD.** The banner sits at the bottom of the chat (above the composer);
   the expanded list anchors to the pill's TOP edge and grows upward. Never downward.
2. **Harness-page toggle.** A switch on the Harness page ("后台任务横幅", default ON) hides the
   banner entirely for users who don't want it. Global (not per-session), persisted server-side
   (same `state_meta` family as other workbench prefs — lane picks the exact key, declares it).
   When off, the banner never renders; the underlying data/API is unaffected.
3. **Length discipline.** The pill has a max width (~420px); the first-item summary truncates
   with ellipsis; the count badge is always visible. The expanded list has a max height
   (~340px ≈ 5 rows); more items scroll inside the popover.
4. **Row navigation = Harness with an automatic SESSION filter.** Clicking a Watch/task row
   opens the Harness page's matching tab with a session filter applied (route param, e.g.
   `?session=<id>`), shown as a removable chip ("仅看:本会话") — WITHOUT the filter the page
   would show everything, which is wrong coming from a session context. A delegated-run row
   opens the runs view similarly filtered/anchored to that run. **This adds Harness-page scope:
   the route param + filter chip + filtered queries are part of this PR** (declare the files).
5. Ordering inside the popover: active/running first, then by start time descending.

## Non-goals (this phase)

- No execution-layer unification (P2, shelved).
- No controls on banner rows (pause/cancel/remove stay on their own pages).
- No mobile-specific redesign — the banner behaves as today, just with more sources.
