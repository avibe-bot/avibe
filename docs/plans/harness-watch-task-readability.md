# Harness Watches & Tasks — state-first rows

Status: **approved, unblocked** — owner sign-off 2026-07-26.
Owner decisions: D3 = replace the enabled/disabled filter with a lifecycle
vocabulary and align the tab badges; D4 = hide the Webhooks placeholder tab.

Companion spec: `docs/plans/harness-runs-readability.md` (LANE R1), merged as
`3c00490365` ("feat(workbench): readable harness run rows with linkable
sessions (#1023)"). R1 fixed the same class of defect on the Runs tab. **Branch
from a master that contains it** — all three tabs live in one component, and R2
reuses R1's title helper, duration helper, and `_session_summary` semantics.

## 1. Problem

Three defects, each verified against both the rendered UI and the live store.

**1.1 A waiting watch shows no time, and a dead waiter looks alive.**
`HarnessPage.tsx:1034` renders the watch timestamp as
`{watch.last_event_at && (...)}` — the whole block disappears when the field is
null. A one-shot waiter produces no output until it fires, so `last_event_at`
stays null for exactly the watches that have been waiting longest. Measured:
**3 of 4 enabled watches render a blank timestamp**, while `last_started_at` is
populated on all 4.

The backend already knows better: `list_watches_page` *orders* by
`coalesce(last_event_at, last_started_at, updated_at, created_at)`. The fallback
chain exists for sorting and was never applied to display.

Liveness is also already stored — `write_watch_runtime` persists a
`runtime:<watch_id>` row carrying `running` and `pid` — and is shown in the
Watch detail panel but not in the list. A crashed waiter and a healthy one are
visually identical.

**1.2 The watch row spends its second line on mechanism.**
The subtitle is the raw start command: for 3 of 4 enabled watches the full
`wait_pr.py` argv including absolute developer paths, for the 4th a 790-character
inline `bash` loop. The detail panel already carries this verbatim.

**1.3 Task rows are titled by a hash, and never say when they run next.**
40 of 54 live scheduled definitions have no `name`, so the row falls back to the
id — the same defect R1 fixes on Runs. Time is printed raw: `17 10 * * *` for
cron, `2026-07-26T13:35:00+08:00` for one-shots. `HarnessPage.tsx:809` gates the
timestamp on `last_run_at`, so a task that has never run shows nothing at all —
and **next fire time is absent from the row entirely**, which for a scheduler is
the one question worth answering.

**1.4 (cross-cutting) The status axis conflates two different things.**
`全部 / 仅启用 / 仅禁用` puts "I paused this" and "it finished on its own" in one
bucket. The second case dominates: of 1,180 disabled watches, **1,156 are
one-shot waiters that fired and retired**. Calling them "disabled" is wrong —
they completed. The tab badges compound it by counting the whole population
(54 / 1,174) next to a default view showing 4 and 4.

## 2. Measured facts (2026-07-26, developer machine)

The lifecycle state is **fully derivable from existing columns**. No migration.

| State | Derivation | Measured |
| --- | --- | ---: |
| `running` | an in-flight run exists for this definition | live |
| `waiting` | `enabled = 1` and no in-flight run | watches 4, tasks 3 |
| `paused` | `enabled = 0` and it did **not** end on its own | 4 |
| `finished` | `enabled = 0` and it ended on its own | 1,228 |

`paused` vs `finished`, per type:

| Type | Rule for `finished` | Rows |
| --- | --- | ---: |
| watch, `mode = once` | `last_finished_at IS NOT NULL` | 1,156 |
| watch, `mode = forever` | `last_finished_at IS NOT NULL` | 22 |
| scheduled, `schedule_type = at` | `last_run_at IS NOT NULL` | 50 |
| watch, `mode = once`, never fired | → `paused` | 2 |
| scheduled, `schedule_type = cron`, disabled | → `paused` | 2 |

Within `finished`, the exit tells three different stories — and today all three
render as "禁用":

| Detail | Rule | Rows |
| --- | --- | ---: |
| `normal` | `last_exit_code = 0` | 5 + 50 + 1,156 |
| `timeout` | `last_exit_code = 124` (lifetime timeout — the designed end for a `forever` watch that never fired) | 14 |
| `error` | any other non-zero exit (`rc=7` api_error, spawn failure, utf-8 decode crash) | 3 |

**A watch that timed out or crashed instead of firing is currently
indistinguishable from one the user switched off.** That is the finding worth
surfacing, and it costs one word on the row.

Cron shapes in the store — only 4 distinct expressions, 3 of them `M H * * *`.
A small formatter covers them; **no new dependency is needed**, and APScheduler
(already a dependency, `pyproject.toml:48`) computes next fire times.

## 3. Contract — new fields on the task/watch payload (FROZEN)

Produced by `storage/background.py`, consumed by `HarnessPage.tsx`. Present on
list and detail for **both** `scheduled` and `watch` definitions.

```
lifecycle_state:  'running' | 'waiting' | 'paused' | 'finished'
lifecycle_detail: 'normal' | 'timeout' | 'error' | null   # non-null only when finished
next_run_at:      string | null    # ISO-8601; cron via APScheduler CronTrigger, one-shot = run_at
waiting_since:    string | null    # last_started_at, when waiting
running_since:    string | null    # started_at of the in-flight run, when running
process_alive:    boolean | null   # watches only; from the runtime:<watch_id> row. null = unknown
```

`running_since` was added during review, and is the one change to this frozen
list. It cannot be `last_started_at`: that column is the *definition's* last
cycle, so a `forever` watch that fired yesterday and began a fresh run a minute
ago would report a day of running — a duration stitched together from two
cycles. It comes from the same in-flight `agent_runs` row that made the state
`running`, and is `null` while that run is merely queued, because a queued run
has not started and there is no duration to show.

Counts gain per-state buckets so the filter chips and the tab badge can each
show a real number:

```
counts: { running, waiting, paused, finished, total }
```

**Non-goals.** No new columns, no migration, no change to existing payload keys.
The row **title** is computed client-side by reusing R1's fallback helper — it is
deliberately not a server field.

### 3.1 Dependency on R1 — read before implementing

`write_watch_runtime` stores watcher liveness as `watch_runtime` run rows keyed
`runtime:<watch_id>`. R1 hides that run type from the **Runs list by default**
(D1). Hiding is a list filter, not a deletion: these rows remain the source for
`process_alive`. Do not "clean them up", and do not read liveness from anywhere
else.

## 4. Behavior

### 4.1 State vocabulary (D3)

The filter offers five buckets — `全部 / 在等 / 在跑 / 已暂停 / 已结束` — each with
its own count. Default view = `在等 + 在跑`.

Shipped as six chips: those five plus `进行中` for the default view itself.
Without it the landing view is the one view the user cannot return to after
clicking away — the filter would offer every state except the one the page opens
on. It is a filter without being a state, so §4.4's badge still reads the
default view's count off the same table.

The row prints the precise word from the same vocabulary, so `finished` reads as
one of **正常结束 / 已超时 / 出错结束**. No extra filter chip: one concept, the
filter groups, the row specifies. An `error` row is visually marked; `timeout`
is marked distinctly from `normal` because a watch that timed out never did its
job.

### 4.2 Row anatomy

Line 1: state dot · title · `一次性`/`持续` chip (from `mode`, watches) or
schedule chip (tasks).
Line 2 carries **state, not mechanism**:

| Case | Line 2 |
| --- | --- |
| watch, waiting, `once` | `已等 <since waiting_since>` · `进程在跑` / `进程已退出` |
| watch, waiting, `forever` | `最近一次 <last_event_at>` · liveness |
| watch, finished | `<正常结束 / 已超时 / 出错结束>` · `<last_finished_at>` |
| task, waiting, one-shot | `还有 <until next_run_at>` · `<humanized next_run_at>` |
| task, waiting, cron | `下次 <humanized next_run_at>` · `上次 <last_run_at>` |
| any, paused | `已暂停` · `<last activity>` |

Time is never printed raw. `17 10 * * *` → `每天 10:17`;
`2026-07-26T13:35:00+08:00` → `今天 13:35`. The raw cron expression and the full
timestamp stay in the detail panel.

Never gate a whole block on one nullable field (defect 1.1). Fall back through
`waiting_since` → `last_event_at` → `updated_at`, mirroring the ordering
`coalesce` that `list_watches_page` already uses.

### 4.3 Title

Reuse R1's fallback helper verbatim:
`name` → first non-empty line of the message → type label. Truncate with CSS,
not by slicing — detail and search need the full text.

### 4.4 Tab badge

The badge shows `waiting + running` — how many things are working for the user
right now. It must never show a total the default view excludes.

### 4.5 Linkability parity — inherited from R1

`core/services/agent_graph.py::openable_in_chat` is looser than the rule Tasks
and Watches apply when deciding whether a row's session is openable. R1 found
it, correctly left it alone (cross-surface, outside its scope), and handed it
here — Tasks/Watches linkability is this lane's subject.

Reconcile the two into one predicate rather than copying either. A row that
offers a chat link must open a session that exists and is reachable; a row whose
session was deleted says so (R1 made `_session_summary` honest about this — do
not re-introduce the delivery-key fallback for a run that carries a concrete
`session_id`). Same rule on both surfaces, declared once.

**One projection site R1 left raw**, found in the post-merge browser pass and
assigned here because it is the same predicate: the Run detail panel's 来源
field prints `source_actor` verbatim, and when `source_kind == 'agent'` that
value is a session id — `ses53w9zb8ba6` where a title and a link belong. It is a
real `agent_runs` column, so R1's "strings the projection invents" enumeration
test correctly did not flag it; a raw column can still be a foreign key to a
name. Resolve it through the same summary path, apply the same linkability
predicate, and extend the enumeration to cover id-shaped columns, not only
invented fields.

### 4.6 Webhooks (D4)

Remove `webhooks` from `TabKey` / `TAB_ORDER` and delete `WebhooksEmpty` and its
icon branch (`HarnessPage.tsx:69, 71, 218, 412, 497, 509, 633, 699`). Deep links
carrying `?tab=webhooks` must fall back to `tasks` rather than render blank.

## 5. Implementation notes

- One enrichment chokepoint per type, batched over the page — mirror R1's
  `_enrich_runs`. A 30-row page must not issue 30+ queries; `process_alive` is
  one batched lookup over `runtime:<id>` for the page's watch ids.
- Next fire time uses APScheduler's `CronTrigger.get_next_fire_time()` with the
  definition's stored `timezone`. Do not hand-roll cron arithmetic.
- The cron humanizer handles `M H * * *`, `M H * * <dow>`, `*/N * * * *`,
  `* * * * *`, and falls back to the raw expression. Keep it a pure function
  with unit tests; **do not add a dependency for it**.
- Frontend: reuse R1's title and duration helpers, the existing filter row, the
  existing `DetailSession`, and `ui/src/components/ui/` primitives. All new
  strings via `ui/src/i18n/en.json` + `zh.json`.

## 6. Evidence expected

- **Unit (python):** the full state-derivation matrix from §2, including all
  three `finished` details and both `paused` cases; `next_run_at` for cron
  (across a DST boundary) and one-shot; `process_alive` true / false / unknown;
  batched (no N+1); per-state counts consistent with the listed rows.
- **Unit (ui):** cron humanizer for each supported shape plus the raw fallback;
  a watch with `last_event_at = null` still renders a time; a `?tab=webhooks`
  deep link falls back to `tasks`.
- **Build gate:** `cd ui && npm run build`.
- **Residual manual:** real-browser check — the three enabled watches show
  elapsed time and liveness; a task row shows its next fire time; each filter
  chip's count matches the rows it lists. Deferred to the orchestrator's
  integration pass.
