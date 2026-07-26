# Harness Runs — readable rows, linkable sessions, working filters

Status: **approved, in implementation** (owner sign-off 2026-07-26)
Owner decisions: D1 = default-hide `watch_runtime`; D2 = message first line may be used as the row title.

## 1. Problem

The Harness → Runs surface is the only harness view that never received a
human-readable projection. Tasks and Watches were enriched with a resolved
session summary and given search + status filters (#431). The Agents graph got
`openable_in_chat` (#956). Runs still renders the raw `agent_runs` row:

- **Row headline is the run id hash.** `8246ff808e8c` tells the user nothing.
  The only other text on the row is the agent name and a relative timestamp.
- **The bound session is dead grey text.** `RunDetail` prints `run.session_id`
  as a `<code>` because the runs payload carries neither the session title nor
  any signal about whether that session can be opened. This silently deviates
  from `docs/plans/agents-run-graph-and-session-visibility.md` Part B, which
  specified "`session_id` + open-chat link"; the implementation retreated to
  plain text out of dead-link fear.
- **No filters at all.** `showSearchBar` is gated to `tasks | watches`, and the
  status filter is explicitly dropped for runs (`status: tab === 'runs' ?
  undefined : statusFilter`) — even though `GET /api/harness/runs` already
  accepts `status`, `run_type`, `agent_name`, `definition_id`, and `query`.
  11k+ rows are navigable only by paging 30 at a time.

Root cause is single: **the runs payload is unprojected.** Both visible
symptoms fall out of that one gap, and the fix is to reuse the projection the
sibling surfaces already have rather than to special-case the frontend.

## 2. Ledger facts (measured 2026-07-26, developer machine, 11,230 runs)

| `run_type` | rows | has `session_id` | has `definition_id` | has message text |
| --- | ---: | ---: | ---: | ---: |
| `watch` | 3,636 | 3,629 | 3,636 | 3,636 |
| `watch_runtime` | 3,005 | 0 | 3,004 | **0** |
| `hook_send` | 2,995 | 110 | 0 | 2,995 |
| `agent_run` | 1,384 | 1,384 | 0 | 1,384 |
| `scheduled` | 208 | 52 | 208 | 208 |
| `task_run` | 2 | 0 | 2 | 0 |

Two facts drive the design:

1. **Every run type except `watch_runtime` and `task_run` carries message
   text.** A single uniform title rule therefore covers ~99.97% of rows; no
   per-type title logic is needed.
2. **Zero runs point at a `private_agent_run` pseudo-scope session** (M1's
   re-parenting is complete). The only real dead-link risk is **422 runs whose
   `session_id` names a session row that no longer exists** — which a server-side
   resolve detects exactly, per row.

## 3. Contract — new fields on the run payload (FROZEN)

Produced by `storage/background.py`; consumed by `ui/src/components/workbench/HarnessPage.tsx`.
Present on **both** `GET /api/harness/runs` (list) and `GET /api/harness/runs/<id>` (detail).

### 3.1 Bound session — reuse `HarnessSessionSummary` verbatim

The five existing fields, same names, same semantics, produced by the **same**
`SQLiteBackgroundTaskStore._session_summary(conn, session_id, session_key, deliver_key)`
that already backs `_enrich_task` / `_enrich_watch`:

```
session_title:        string | null
session_platform:     string | null
session_scope_kind:   string | null
session_label:        string | null
session_is_workbench: boolean
```

Consequences that are **by design**, not gaps:

- A workbench session resolves with `session_is_workbench = true` and a title →
  the existing `DetailSession` component links it to `/chat/<session_id>`.
  **This is the owner's headline fix and it requires no new frontend logic.**
- An IM-scope session shows platform + channel and is **not** linked, exactly as
  on Tasks/Watches today. Workbench chat renders workbench sessions; IM
  transcripts are scope-keyed (#535), so an unconditional `/chat` link would
  open an empty transcript. Out of scope here: the Agents graph's laxer
  `openable_in_chat` (`not is_private_run`) is inconsistent with this rule —
  report it, do not change it in this lane.
- A **deleted** session resolves to the all-null summary. The frontend must then
  render an explicit "session deleted" label, not a bare hash — see §4.2.

### 3.2 Originating definition

```
definition_name:    string | null   # run_definitions.name, even when soft-deleted
definition_kind:    'task' | 'watch' | null   # from run_definitions.definition_type
definition_deleted: boolean         # run_definitions.deleted_at is not null
```

### 3.3 Callback (report-back) session

```
callback_session: HarnessSessionSummary | null
```

A nested object rather than five more prefixed fields, so it drops straight into
`DetailSession` alongside the primary session. `null` when `callback_session_id`
is null.

### 3.4 Non-goals for the contract

No new columns, no migration, no change to `_run_from_row`'s existing keys.
Nothing is removed from the payload. `count_runs` / `count_runs_by_status` keep
their shape; they only gain the `exclude_run_type` filter argument (§4.3) so
their counts stay consistent with what the list shows.

## 4. Behavior

### 4.1 Row title — one uniform rule

```
title = first non-empty line of `message` (trimmed, collapsed whitespace)
     || definition_name
     || run_type label
```

Truncate to one line with CSS, not by slicing the string (the detail panel and
the search index must keep the full text). D2 explicitly permits the message
first line to appear in the list.

Row anatomy (replacing the hash headline):

- **Line 1:** status icon · title · run-type chip
- **Line 2:** trigger chip (`definition_name`, links to its Watch/Task when
  `definition_kind && !definition_deleted`) · agent name · session label ·
  duration · relative time
- The run id moves to the detail panel. Keep it copyable there.

### 4.2 Session field — link, label, or honest dead end

| Resolved state | Render |
| --- | --- |
| workbench session | `DetailSession` → link to `/chat/<id>` (title as label) |
| IM session | `DetailSession` → platform icon + channel label, not linked |
| `session_id` set, nothing resolved | muted "session deleted" label + the id, not linked |
| no `session_id` | existing `harness.detail.sessionNone` |

New i18n key required for the deleted case (en + zh).

### 4.3 Filters on the Runs tab

Reuse the Tasks/Watches filter row — do not build a second one.

- **Search** — bind to the existing `query` param. Remove the
  `showSearchBar = tab === 'tasks' || tab === 'watches'` gate.
- **Status** — stop dropping it for runs (`status: tab === 'runs' ? undefined :
  statusFilter`); `count_runs_by_status` already feeds the counts.
- **Run type** — a type selector bound to the existing `run_type` param.
  `HarnessRunsParams.runType` is already typed.
- **One new API param, `exclude_run_type`** (comma-separated) — required by D1
  and the only server-side addition. `run_type` is an equality match, so
  "everything except heartbeats" is not expressible today. An exclusion param
  keeps the default future-proof: a run type added later shows up by default
  instead of silently vanishing from a hardcoded include-list. Thread it through
  `_runs_query`, `list_runs_page`, `count_runs`, `count_runs_by_status`, the
  `/api/harness/runs` handler, and `HarnessRunsParams.excludeRunType`.

### 4.4 D1 — `watch_runtime` hidden by default

`watch_runtime` rows are watcher-process heartbeats: 3,005 rows (27%), no
session, no agent, no message. Their content (pid, start time) is already shown
in the owning Watch's detail panel.

- Default state of the Runs tab sends `exclude_run_type=watch_runtime`.
- The run-type selector can bring them back explicitly (selecting the
  `watch_runtime` type clears the exclusion).
- The default must be **visible and reversible in the UI**, never a silent
  truncation: the type selector shows which types are being shown.
- Status counts must stay consistent with what is listed (the same `run_type`
  filter feeds `count_runs_by_status`).

## 5. Implementation notes

- **Enrichment chokepoint:** add one `_enrich_runs(rows, conn)` to
  `SQLiteBackgroundTaskStore`, called from `list_runs_page` and `get_run`. It
  performs **two batched queries per page** over the page's distinct ids — one
  for sessions (reusing `_session_summary`'s join, or a batched variant of it),
  one for definitions. No N+1: a 30-row page must not issue 30+ queries. Add a
  test that asserts the query count or at least that a page of N rows resolves
  through a batched path.
- Do **not** enrich `_run_from_row` (a staticmethod with no connection) and do
  not enrich the SSE publish path — `_publish_run_rows_updated` emits a thin
  notification and the UI responds with a full refetch, so there is no
  stale-row-blanking hazard.
- Frontend: reuse `DetailSession`, the existing filter row, and
  `ui/src/components/ui/` primitives. New user-visible strings go through
  `ui/src/i18n/en.json` + `zh.json`.

## 6. Evidence expected

- **Unit (python):** `_enrich_runs` resolves workbench / IM / deleted-session /
  no-session cases; definition name + kind + deleted flag; batched (no N+1);
  `watch_runtime` filtering leaves counts consistent.
- **Unit (ui):** title fallback chain (message → definition name → run type);
  deleted-session renders the label, not a link.
- **Build gate:** `cd ui && npm run build`.
- **Residual manual:** real-browser check of the Runs tab — a watch run's
  session link opens the right chat; the type selector restores heartbeats;
  search + status narrow the list. Deferred to the orchestrator's integration
  pass.
