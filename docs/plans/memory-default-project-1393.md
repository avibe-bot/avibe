# Memory default project and explicit project IDs

> Status: accepted
> Issue: #1393
> Branch / worktree: `plan/memory-default-project` at
> `/Users/rk/work/chainbot/avibe-bot/worktrees/memory-default-project`
> Base: `origin/master` @ `b6c4393f`

This revision absorbs the plan review: schema v3, split project-id
predicates, a durable project catalog, a bounded UI-only `all`
contract, multi-project final flush, and a closed ID input matrix.

## Background

#1393 is not a lost-index bug. User conversations are captured under an
EverOS `project_id` derived from the Agent Session workdir
(`p-<32 hex>`). Settings Search then hashes the **default Agent cwd**
and queries that space. A Workbench project whose folder is not the
default cwd returns HTTP 200 with `items: []`.

EverOS treats `project_id` as a hard partition. Search always pins
`project_id = ?`. The literal `"default"` is EverOS's reserved default
space (on disk: `default_project`), not a cross-project view. Avibe's
sidecar currently rejects `"default"` on add/search and only allows
`p-[0-9a-f]{32}`. Profile `/get` already uses `"default"`.

The current store CHECK (`core/memory/schema.sql` `project_ref`, schema
version 2) also only accepts `p-<32 hex>`. Python-only relaxation of
`is_project_id` cannot persist `"default"` or named slugs.

Upstream EverOS 1.2.3 has no global search. Episode LanceDB ids omit
`project_id` (EverOS #320). Multiple EverOS projects for one owner
remain a residual index-collision risk; this iteration reduces how
often that happens, it does not change EverOS key shape.

## Goal

Close #1393 by putting all user conversation capture on EverOS
`"default"`, and by making extra projects **opt-in named slugs**.

- User conversations always write `project_id = "default"`.
- Agent `remember` may pass a named slug. Omitted → `"default"`.
- Settings and Agent search this principal only. No `u-…` fan-out.
- Agent search: `"default"` or a named slug. No `all`.
- Settings Search: picker default `"default"`; may choose a named slug
  or **all of this user's catalogued projects**.
- Legacy `p-<32 hex>` is not a product id. It is only a persisted
  recovery/drain shape.

## Non-goals

- Cross-user admin search.
- Mapping Workbench `proj_…` or workdir onto EverOS `project_id`.
- EverOS upstream global search.
- Migrating historical `p-…` markdown into `"default"`.
- Changing profile ownership. Profile stays per principal.
- Patching EverOS row ids this iteration.

## Project ID contract

Single module: `core/memory/project_ids.py`. No second charset.

Wire values must already be lowercase. **Do not `casefold()`.** Mixed
case is invalid.

| Token | Shape | New write | Agent search | UI search | Persist / drain |
|---|---|---|---|---|---|
| `default` | exact | yes | yes (CLI default) | yes (picker default) | yes |
| named slug | `^[a-z][a-z0-9_-]{0,62}$`, not reserved, not `p-` prefix | yes | yes | yes | yes |
| `all` | exact | no | no | yes | no |
| `p-[0-9a-f]{32}` | exact legacy | no | no | no | yes |
| `personal` | reserved | no | no | no | no |

Reserved: `default`, `all`, `personal`. Named slugs must not start with
`p-`.

### Input matrix

| Input | remember / user capture | Agent search | UI search |
|---|---|---|---|
| key omitted / JSON `null` | `default` | `default` | `default` |
| `""` / whitespace | invalid | invalid | invalid |
| `default` | `default` | `default` | `default` |
| `Default` / `BILLING` | invalid | invalid | invalid |
| `billing` | create/use `billing` | `billing` if catalogued, else invalid | same |
| `all` | invalid | invalid | UI `all` fan-out |
| `personal` | invalid | invalid | invalid |
| `p-` + 32 hex | invalid | invalid | invalid |
| `p-deadbeef` (short) | invalid | invalid | invalid |
| `u-…` | invalid | invalid | invalid |

Helpers:

- `is_legacy_memory_project_id` — exact `p-[0-9a-f]{32}`
- `is_new_stored_memory_project_id` — `default` or named slug
- `is_persisted_memory_project_id` — legacy **or** new stored
- `is_writable_memory_project_id` — new stored only
- `parse_writable_memory_project`
- `parse_agent_search_project` — new stored only
- `parse_ui_search_project` — new stored or `all`

Callsites must pick a predicate. **Do not** make `store.is_project_id`
a drop-in alias for persisted and keep using it on new write/read
paths.

| Callsite | Predicate |
|---|---|
| New capture (`CaptureRequest` / `module.capture`) | `is_writable_memory_project_id` |
| `enqueue_request` | `is_writable_memory_project_id` |
| `module.recall` / `module.profile` | `is_new_stored_memory_project_id` |
| `provider_session_ref`, `resolve_current_session_scopes`, worker drain, session lifecycle | `is_persisted_memory_project_id` |
| Sidecar add/flush | persisted |
| Sidecar search | new-stored |

If `store.is_project_id` remains as a compatibility name, it may only
wrap persisted and **must not** be called from capture, enqueue,
recall, or profile. Grep those four sites in the implementation PR.

`derive_project_id(scope_key, workdir)` stays for old tests. New
capture and remember must not call it.

## Schema v3 (P0)

Current `MEMORY_STORE_SCHEMA_VERSION = 2`. Fresh `schema.sql` and
existing v2 files must both accept the new project shapes.

### Fresh schema

- Bump `PRAGMA user_version` to 3.
- Widen `memory_capture_queue.project_ref` CHECK to
  `is_persisted_memory_project_id` (SQL equivalent of `default` OR
  exact `p-<32 hex>` OR named slug).
- Add catalog:

```text
memory_projects (
  principal_id TEXT NOT NULL,   -- same u- CHECK as the queue
  project_id   TEXT NOT NULL,   -- new-stored CHECK only (no legacy)
  created_at   TEXT NOT NULL,
  last_written_at TEXT NOT NULL,
  PRIMARY KEY (principal_id, project_id)
)
```

- Include `memory_projects` in `_MEMORY_STORE_TABLES` /
  `_verify_current_schema`.

### v2 → v3 migration

Invariant: **no data loss and the final schema object set matches
v3**. Temporary drop/recreate of queue indexes during the rebuild is
required (SQLite keeps the old index names on the renamed table).
Do not drop settlements, attachments, or their triggers.

One transaction:

1. Drop `ix_memory_capture_due` and
   `ix_memory_capture_session_generation`.
2. `ALTER TABLE memory_capture_queue RENAME TO memory_capture_queue_v2`.
3. `CREATE TABLE memory_capture_queue` with the v3 `project_ref` CHECK
   and the same columns otherwise.
4. `INSERT INTO memory_capture_queue SELECT * FROM memory_capture_queue_v2`
   (every row, including `p-…`).
5. `DROP TABLE memory_capture_queue_v2`.
6. Recreate `ix_memory_capture_due` and
   `ix_memory_capture_session_generation`.
7. `CREATE TABLE memory_projects` if missing.
8. Backfill catalog from distinct `(principal_id, project_ref)` **only
   where `project_ref` is new-stored**. Never insert `p-…`.
9. Execute `PRAGMA foreign_key_check` and require zero returned rows;
   then `_verify_current_schema`; then `PRAGMA user_version = 3`.

Settlement triggers (`trg_memory_flush_settlements_*`) must still
exist after the transaction.

Open path (each step is a real snapshot, not a rename of
`schema.sql`):

| On-disk `user_version` | Action |
|---|---|
| 0, empty | Install current `schema.sql` (v3) |
| 0, recognized non-empty v0 | Keep a frozen v2 `schema.sql` snapshot in-repo. `_migrate_v0_to_v2` applies **that snapshot only**, then `_migrate_v2_to_v3`. Do not point v0→v2 at live `schema.sql` after it becomes v3. |
| 1 | existing `_migrate_v1_to_v2`, then `_migrate_v2_to_v3` |
| 2 | `_migrate_v2_to_v3` |
| 3 | verify only |
| other | fail closed |

Check in `tests/fixtures/memory_foundation_v2.sql` (released v2
shape). Do not generate it from post-change `schema.sql`.

### Legacy non-terminal rows

`pending` / `processing` / `manual_required` rows with `p-…` stay
drivable:

- Worker add/flush still sends that legacy `project_id` to EverOS.
- Sidecar add/flush therefore **accepts persisted ids** (legacy +
  new stored) so drain can finish.
- Sidecar search accepts **new stored only**.
- New public remember/search/UI never accept `p-…`.
- After drain, terminal `p-…` tombstones may remain until ordinary
  compaction. They are invisible to picker and `all`.

### Tests

- Released v2 fixture (check in a v2-shaped file, or generate from
  the current v2 `schema.sql` snapshot) loads, migrates, keeps a
  `p-…` queue row, and then accepts a `"default"` enqueue.
- Fresh install is user_version 3 and can enqueue `"default"` and
  `billing`.
- A v2 file with a non-terminal `p-…` row still recovers scopes and
  can be claimed.

## Durable project catalog

Source of truth for picker, `all`, and “unknown slug”.

- Created/updated in the **same enqueue transaction** as an accepted
  capture or remember of a new-stored project. Duplicate remember
  upserts `last_written_at`.
- `"default"` is always implicitly searchable. Upsert it on first
  user capture or default remember; picker always shows it even if
  the row is missing.
- Named slug remember of a not-yet-catalogued id **creates** the
  row (that is how a project is created).
- Named slug **search** of a not-yet-catalogued id is
  `memory_invalid_input` (unknown).
- Cap: 16 named projects per principal, plus `"default"`. A 17th
  distinct named remember is `memory_invalid_input`.
- Clear / factory reset deletes `memory_projects` with the other
  Memory tables.
- Picker order: `default`, named by `last_written_at` desc, then
  UI-only `all`.
- `all` = implicit `default` + catalogued named slugs (see Read path).

## Write path

### User conversations

`CaptureAdmission.project_for` returns `"default"` and does not
require a workdir. Human Workbench and bound IM turns enqueue
`"default"` and upsert the catalog.

### Agent remember

`POST /internal/memory/remember`: `{text}` or `{text, project}`.

- Missing / JSON `null` → `"default"`.
- Present → `parse_writable_memory_project` (rejects `all`,
  `personal`, `p-…`, mixed case, empty).
- Principal and session come from the admitted CLI session, not
  from the workdir hash.

```bash
vibe memory remember "<text>" [--project <slug>] [--json]
```

Idempotency:
`agent:{principal}:{project}:{session}:{digest}`.

### Final flush (P1)

`final_flush_memory_cli_session` must flush **every** current-epoch
scope of that raw session, not only live `(principal, "default")`.

- Union live CLI scope with `resolve_current_session_scopes()`.
- Order: `"default"` first, then remaining project ids sorted.
- **One shared deadline** (controller default 5s, same as today's
  single-scope budget). Compute an absolute deadline at entry.
  Each scope gets only remaining time. After the deadline, do not
  start another provider call; mark leftover scopes unfinished.
- Prefer a `final_flush_scopes(...)` helper that reuses the
  multi-scope admission-lock pattern from archive.
- If any visited scope with unflushed work fails, or any scope was
  skipped because the budget expired, the overall result is failure.
- `archive_memory_cli_session` uses the same union + shared deadline.
- Tests: (1) remember `--project billing` then final-flush visits
  `billing` and `default` if in the union; (2) several slow scopes
  still finish (or abort) within ~5s wall time, not 5s × N.

### System prompt

- `remember` / `search` without `--project` use `"default"`.
- `remember --project <slug>` only when the fact is explicitly for
  that named space.
- `search --project <slug>` only that space.
- Never `--project all` (Settings only).
- Never hashes, never Workbench `proj_`, never `personal`.

## Read path

Principal is always server-side:

- Settings: verified UI user key → one principal.
- Agent CLI: admitted session → that principal.

Callers never submit `u-…`.

### Search request

```text
{ query, policy }                 # → default
{ query, policy, project }
```

- UI user key: `parse_ui_search_project`.
- CLI session: `parse_agent_search_project` (`all` → 400).

```bash
vibe memory search "<query>" [--project <slug>] [--limit 1..20] [--json]
```

### Universes

| Selector | Who | EverOS queries |
|---|---|---|
| omitted / `default` | UI and Agent | one `(principal, default)` |
| named slug | UI and Agent | one `(principal, slug)` if catalogued |
| `all` | Settings UI only | implicit `default` + catalogued named slugs |

### `all` membership

```text
all = implicit default + catalogued named projects
```

Always search `"default"` first, even if that principal has no
`memory_projects` row for `default` (user created only named slugs).
Then named slugs by `last_written_at` desc. Never include `p-…`.

### `all` recall contract

Applies only to Settings `project=all`.

1. **Modes.** `keyword`, `vector`, `hybrid`, `auto`. Reject `agentic`
   and reject `include_current_session=true`.
2. Force `include_current_session=false`.
3. Probe provider health / embed capability **once**.
4. `include_profile` is sent on **at most one** project query (the
   `default` query). Every other fan-out call sets
   `include_profile=false`.
5. Fan-out sequentially under the existing lifecycle lock. Overall
   deadline 20s (must finish inside the 45s search transport).
6. Per-project provider failure: skip, continue. If every project
   fails, return the first error. If any succeed, return merged
   items plus `warnings` including `memory_search_partial`.
7. Deadline hit: return merged items so far plus
   `memory_search_truncated`.
8. Merge: collect items, attach `project`, sort by `date` desc
   (missing last), then `kind`, then `text`; dedupe
   `(kind, text, date, project)`; bound to `max_results`.
9. UI shows the project badge on each hit when the selector is not
   the single default space. Partial/truncated banners must render.

### Search warning codes

`memory_search_partial` and `memory_search_truncated` are
**transport-only** warnings, not persisted queue errors.

- Add a closed `MemoryWarningCode` (preferred) or a transport-only
  extension of the public warning union. Do **not** add them to
  SQLite `last_error` CHECKs.
- `RecallItems.warnings` / public search JSON use that warning
  union, not `MemoryErrorCode`.
- TypeScript `MemoryRecallResult` narrows the same two strings.
- `MemorySearchPanel` renders a banner for each present warning
  (EN/ZH i18n + `memoryCopyContract`).
- Tests: API returns the codes on injected per-project failure and
  on an injected deadline; UI contract asserts the banner.

`MemoryItem` / search JSON gain an optional `project` field. Single-
project searches set it to that project.

Do not fan-out other principals.

### Settings UI

`MemorySearchPanel` `<Select>`: Default, named slugs, All my
projects. Default selected.

`GET /api/memory/projects` returns this principal's catalog rows
plus the implicit `default` and the UI-only `all` option. No host
paths. No other principals.

i18n: `ui/src/i18n/en.json` + `zh.json`,
`memoryCopyContract.test.ts`, `vibe/i18n/en.json` + `zh.json`.

## Sidecar and insight

- Add/flush: `is_persisted_memory_project_id` (drain legacy + new
  stored). Reject `all`, `personal`, malformed.
- Search: `is_new_stored_memory_project_id` only.
- Profile `/get` stays `project_id="default"`.

Insight split (admin list currently filters with `_memcell_scope`):

- `_memcell_scope` accepts **persisted** ids (legacy + new stored)
  so admin log can still show historical `p-…` memcells.
- User-scoped `_validated_scope` / scoped list+detail accept only
  **new-stored** (`default` or named). A scoped read of a `p-…`
  project is invalid input, not an empty page.
- Admin SQL/GLOB and the post-filter use persisted.
- Picker / `all` still read **only** `memory_projects` (new-stored),
  never insight legacy ids.

## Residual risk: EverOS project collision

Named slugs still share EverOS owner-keyed Lance ids (EverOS #320).
Opt-in + 16-named cap reduces how often two spaces share
`{owner}_{ep_<date>_00000001}`. Markdown paths stay isolated.
**Index isolation is not guaranteed** this iteration.

Accepted residual: two named projects for one principal on the same
UTC day may clobber one Lance episode/profile row. Follow-up is an
EverOS-side key change or a later Avibe rebuild.

Required regression: same principal, two named remembers with the
same text/timestamp class produce distinct Avibe
`(principal, project)` queue identities and distinct EverOS add
`project_id`s. Do not claim Lance-level isolation in that test.

## Residual data

`p-<hash(workdir)>` markdown and tombstones stay on disk. They are
not listed, not in `all`, not searchable, not writable. Drain-only
until compaction. No rebuild in this iteration.

#1393 closes for **new** captures: user turns → `"default"`;
Settings default search → `"default"`.

## Tests and catalog

New capability `memory_search`. IDs in the executable test and PR.

- `MEMORY-SEARCH-001` — Workbench turn whose workdir ≠ default cwd
  captures as `"default"` and Settings default search returns it.
- `MEMORY-SEARCH-002` — `remember --project billing` is absent from
  default search and present in `--project billing`.
- `MEMORY-SEARCH-003` — Settings `project=all` returns this
  principal's `default` + named hits, never another principal,
  never a `p-` space.
- `MEMORY-SEARCH-004` — Agent `search --project all`,
  `remember --project all` / `personal` / `p-…` / mixed case /
  unknown slug search are rejected.
- `MEMORY-SEARCH-005` — v2 store fixture migrates to v3; a
  non-terminal `p-…` row still drains; a new `"default"` enqueue
  succeeds.
- `MEMORY-SEARCH-006` — `remember --project billing` then
  final-flush visits `billing` (and `default` if in the union).

Also:

- Admission: human capture `"default"`; missing workdir still
  admits.
- Sidecar: add `"default"` allowed; add `all` / `personal`
  rejected; add legacy `p-…` allowed (drain); search legacy
  rejected.
- UI route: search forwards only `{query, policy}` or
  `{query, policy, project}`.
- Catalog upsert on accepted remember; 17th named project
  rejected.
- Insight: `"default"` memcells appear in scoped/admin lists.
- Prompt / CLI-example tests if they pin the old search sentence.

`ruff check` on changed Python. `npm run build` if `ui/` changes.

## Close-out

- Implement on a non-draft PR from this worktree (new implementation
  commits, not plan-only).
- PR names the capability, lists `MEMORY-SEARCH-00x`, and states
  unit / contract / scenario / residual manual layers.
- After merge (or when the owner says): close #1393.

In this worktree use
`GH_CONFIG_DIR=$HOME/.config/gh-avibe-bot` (`rkrkrkk`).

## Implementation order

1. `project_ids.py` + input-matrix unit tests.
2. Schema v3 + v2 fixture migration + catalog table.
3. Split project-id validation by callsite: writable for
   capture/enqueue, new-stored for recall/profile, persisted for
   recovery/drain/lifecycle. Admission +
   `default_memory_project_id()` → `"default"`.
4. Sidecar persisted-vs-search split; insight new-stored ids.
5. Remember/search payloads, catalog upsert, CLI `--project`.
6. Final-flush union of all session scopes.
7. UI-only `all` with the recall contract above.
8. Settings picker + i18n.
9. Scenario catalog and remaining tests.
10. Prompt + `docs/CLI.md` / `docs/CLI_ZH.md`.
