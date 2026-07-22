# Agents Run Graph + Session Visibility Decoupling

Status: **APPROVED (owner, 2026-07-23)** — decisions D1/D4/D5 accepted as
recommended; D2 = keep the Harness Runs tab for now; D3 resolved as
**standalone sessions** (see Part C). Implementation lanes: M1 backend
(codex), M2 graph UI (claude).

Design source: `../avibe-docs/design.pen` frames
`vibe-remote — Agents · 运行图 Graph (Dark)` (node `KfgtJ`) and
`Agents · 运行图 — 规格注解 (Spec)` (node `anu5U`). Exported PNGs:
`_tmp/design-exports/KfgtJ.png`, `_tmp/design-exports/anu5U.png` (workspace
root, not committed).

Interface contract (frozen for parallel lanes):
`docs/plans/agents-run-graph-contract.md`.

Related plans: `agent-run-scope-semantics.md` (updated by Part C),
`agent-scope-model.md`, `agent-run-callback-session.md`.

## Background / problems

1. **Agents → Running tab is a flat list.** `RunningAgentsTab` shows live
   backend processes but no lineage: which session spawned which, and where
   results report back. The data already exists on `agent_runs`
   (`source_kind`, `source_actor` = caller session id, `parent_run_id`,
   `callback_session_id`, `callback_run_id`, `metadata_json.caller_context`)
   and is fully persisted for finished runs — nothing renders it.
2. **Harness → Runs duplicates the "what is running" surface** with a raw,
   hard-to-read run list. Users see two half-truths: Agents/Running (live
   processes, no history, no lineage) and Harness/Runs (all runs, no
   relationships, no session links).
3. **Scope conflates placement with visibility.** A `vibe agent run` without a
   scope target creates a synthetic scope
   (`scopes.native_type == "private_agent_run"`, placeholder platform,
   `no_delivery`) purely so the session falls outside every scope-keyed list
   query. Consequences:
   - a foreground session cannot be hidden;
   - a background session cannot be brought to the foreground (only
     `session fork` into a project scope approximates it, creating a new row);
   - background sessions' chat history is unreachable from the UI
     (`openable_in_chat=false` is keyed off the pseudo-scope marker).

## Goals

- One place answers "who is doing what, who started it, who gets the report":
  an interactive **run graph** on the Agents page, covering live **and**
  finished sessions.
- Every node is inspectable (detail panel) and every session — including
  background ones — can open its full chat history at `/chat/:sessionId`.
- Decouple **scope = placement** (which project/channel, workdir defaults,
  settings inheritance, routing) from **visibility = foreground/background**
  (whether the user sees it in session lists/inbox). Both become independently
  mutable.
- Make **standalone sessions** (no project) a first-class model state, with
  the session's own Show Page workspace as its default working directory.
- Converge the "runs" story so `agent_runs` stays the single store and the UI
  surfaces are projections of it.

Non-goals (this batch): multi-user ACLs; changing callback semantics;
removing the Harness Runs tab (owner: keep for now); user-facing "new
standalone session" entry point (model support only); mobile canvas rendering
(mobile degrades to a grouped list).

## Part A — Agents run graph

### Information architecture

- Agents page tabs: `Agent 列表` (definitions, unchanged) and `运行`
  (the graph; **replaces** the flat running list on desktop — D1 approved;
  badge = live session count). Mobile keeps a grouped list view fed by the
  same graph API.
- Filters: 活跃 / 含历史 segmented; time window (default 24h) applies to
  history; project filter (including `独立` bucket); `显示后台会话` toggle
  (default on).

### Graph semantics (all from existing columns)

- **Node = agent session** (`agent_sessions`), plus **trigger chips** for
  Task/Watch definitions (`run_definitions`).
- **Spawn edge** (solid, mint): runs with `source_kind='agent'`;
  `source_actor` (caller session) → `session_id` (spawned/continued session).
  Aggregate multiple runs between the same pair (edge carries run count).
- **Callback edge** (dashed, cyan): `callback_session_id` — the report route;
  state from `callback_status`.
- **Trigger edge** (dashed, violet): `run_type in (scheduled, watch)` via
  `definition_id` → carrying session. Chip click → Harness definition detail.
- **Node status**: live from the running-agents service (`active`/`idle`/
  `orphan`), else latest run terminal status (`queued`/`succeeded`/`failed`/
  `canceled`). History mode adds ended sessions within the time window.
- Roots (user-facing conversations) render with the foreground (eye) marker;
  background sessions show eye-off and dim when ended.

### Node detail panel

Session title + agent/backend/model/effort; facts: project (scope, or 独立),
workdir, visibility (+ inline `移到前台` / `隐藏` action), 启动方
(source_actor link — click selects that node), 汇报到 (callback target),
trigger source; a short per-session run timeline (each row deep-links to the
Harness run record); actions: `打开会话` (primary, `/chat/:sessionId`),
visibility toggle, `取消 Run` (active runs).

### API and frontend

- `GET /api/agents/graph` per the frozen contract doc. Server-side assembly
  in a new read-only `core/services/agent_graph.py`
  (`agent_sessions ⋈ scopes` + `agent_runs` aggregation + running-agents
  liveness merge). Window cap + `truncated` flag.
- Realtime: reuse SSE `runs_updated` / turn events to refetch; no new
  transport.
- Frontend: **@xyflow/react (React Flow)** + a layered left→right layout
  (dagre or elk) — D5 approved. Node card is a React component reusing
  existing `ui/src/components/ui/` primitives; design.pen is the visual
  source of truth.

## Part B — Harness Runs convergence

Principle: `agent_runs` remains the only store; views are projections.

- **The Runs tab stays** (owner D2: keep for now). Same-batch improvements:
  - Run detail renders the missing session/lineage fields
    (`session_id` + open-chat link, `source_kind`/`source_actor`,
    `parent_run_id`, callback fields) instead of hiding them.
  - Graph node detail run rows deep-link to `/harness?tab=runs&run=<id>`;
    existing backgroundActivity deep links keep working.
- Division of labor: **Agents·运行 = collaboration view** (who is working,
  who called whom), **Harness = triggers + audit trail** (Tasks/Watches
  definitions, Runs ledger). Removing the top-level Runs tab is explicitly
  deferred; revisit after the graph has been in daily use.

## Part C — scope / visibility decoupling + standalone sessions

### Model

- `agent_sessions.visibility TEXT NOT NULL DEFAULT 'foreground'`
  ∈ `foreground | background` (+ index; alembic migration).
- **Scope goes back to placement only.** A session either hangs on a real
  scope (workbench project or IM channel) **or has `scope_id = NULL` =
  standalone session (独立会话)** — a first-class state, not an error. The
  `private_agent_run` pseudo-scope is retired for new sessions.
- **Standalone session workdir = the session's own Show Page workspace**
  (`~/.avibe/show/<session_id>`, i.e. what `vibe show path` reports).
  Chicken-and-egg resolution: session ids are minted in-process
  (`storage/agent_session_rows.new_session_id`) **before** the row INSERT —
  the id is not DB-assigned. Creation flow:
  1. mint `session_id`;
  2. compute `workdir = <show workspace root>/<session_id>`;
  3. `mkdir -p` that directory (the full Show Runtime scaffold stays lazy —
     directory existence is enough for a valid cwd; first `vibe show` use
     scaffolds the app as today);
  4. INSERT the session row with the explicit `workdir` (explicit workdir
     already wins over scope snapshot in `create_agent_session_row`).
  Result: the session's work products, files, and its Show Page live in one
  self-contained, git-checkpointed folder per session.
- Placement defaults for `vibe agent run` (supersedes the corresponding rows
  in `agent-run-scope-semantics.md`):
  - no scope param + caller context → **caller session's scope**,
    `visibility='background'`; cwd rule unchanged (caller shell cwd).
  - `--same-scope` / `--scope-id` → as today; visibility defaults to
    `background` for delegated runs; explicit `--visibility foreground`
    opt-in.
  - no caller and no scope (human CLI outside a session) → **standalone**
    (`scope_id=NULL`) + `visibility='background'` + show-workspace workdir.
  - task/watch `create_per_run` sessions → definition's `session_scope_id`
    when present, else standalone; `visibility='background'` unless the
    definition says otherwise (D4 approved).
  - future user-facing "新建独立会话" → standalone + foreground (out of
    scope this batch; the model must support it).
- **Delivery rule**: `visibility='background'` ⇒ suppress outward platform
  delivery (today's `no_delivery`), callbacks unaffected. Promoting to
  foreground restores normal delivery from the next turn on. One rule
  replaces the metadata flag: 前台会说话,后台只干活、走 callback。
- **Query gates** switch from scope-shape heuristics to the flag:
  - workbench session list / `vibe session list` / inbox:
    `visibility='foreground'` (+ existing `status='active'`);
  - running-agents service: `openable_in_chat = true` for every persisted
    session (background included);
  - graph & Harness always see both visibilities;
  - scope joins become LEFT JOINs; NULL scope renders as the `独立` bucket.

### Operations

- API: `PATCH /api/sessions/:id { visibility }`; scope move is a separate
  operation (`{ scope_id }`, may be NULL for standalone) that does not touch
  the session's snapshotted workdir.
- CLI: `vibe session update --visibility foreground|background` and
  `--scope-id <scopes.id>|none`; agent-facing so an agent can surface a
  session it wrongly backgrounded.
- UI: graph detail panel buttons (`移到前台` / `隐藏`); session list row menu
  gets `隐藏会话`; hidden sessions reachable via the graph's
  `显示后台会话` toggle.
- Realtime contract: every PATCH or CLI placement edit publishes the same
  `session.activity` `updated` event with `session_id`, nullable `scope_id`,
  `title`, and `visibility`. Foreground-only list and Inbox consumers remove a
  row when `visibility='background'` and reconcile it when it returns to
  foreground or moves scope. M1 owns the shared backend event path; M2 owns
  the `ui/**` consumption so the two lanes do not edit `ApiContext.tsx` in
  parallel.

### Migration

1. Add column, backfill `foreground`.
2. Sessions on `private_agent_run` scopes → `visibility='background'`;
   re-parent `scope_id` to the caller session's scope via
   `agent_runs.source_actor` / `metadata_json.caller_context` when
   resolvable, **else `scope_id=NULL` (standalone)**. Session anchors are
   self-anchored (anchor = session id) so `(scope_id, session_anchor)`
   uniqueness holds — verify in migration tests. Existing workdirs stay
   untouched.
3. Keep legacy pseudo-scope rows for history until a later cleanup; stop
   creating new ones. `session_fork` stops stripping `private_agent_run`
   metadata (no longer meaningful).
4. Sweep every `private_agent_run` / `no_delivery` read site
   (`running_agents._apply_session_meta`, inbox/message services, delivery
   paths) to the new flag — fix the class, not instances.

## Phasing & lanes

- **M1 (backend, codex lane)**: visibility column + standalone sessions +
  placement defaults + delivery rule + query gates + CLI/API ops + migration
  + tests. No UI dependency. Branch `feat/session-visibility-decoupling`.
- **M2 (graph, claude lane)**: `core/services/agent_graph.py` +
  `GET /api/agents/graph` + Agents 运行 tab (React Flow) + detail panel +
  mobile list fallback + Harness RunDetail session/lineage enrichment.
  Branch `feat/agents-run-graph`. Contract-first; visibility actions call the
  M1 PATCH and degrade gracefully until M1 merges.
- Merge order M1 → M2 (M2 rebases). Mutual no-touch zones and exact file
  scopes are in the lane briefs; both lanes follow
  `.agents/skills/pr-delivery-loop/SKILL.md`.
- Later (separate batch): pseudo-scope row cleanup; Runs tab IA revisit;
  user-facing standalone session entry.

## Decision log (owner, 2026-07-23)

- D1 graph replaces desktop Running list — **approved**.
- D2 Harness Runs tab — **keep for now**; demotion deferred.
- D3 caller-less / project-less sessions — **standalone sessions**
  (`scope_id=NULL`), default workdir = own Show Page workspace directory;
  chicken-and-egg solved by pre-minting the session id (Part C).
- D4 task/watch-created sessions default background — **approved**.
- D5 @xyflow/react dependency — **approved**.
- D6 placement-change realtime ownership — **backend event contract in M1,
  browser consumption in M2**; M1 does not expand into `ui/**` because M2
  already owns the shared event client.
