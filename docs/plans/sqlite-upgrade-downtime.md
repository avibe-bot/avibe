# SQLite upgrades without full-database downtime

Status: implementation contract selected by the owner on 2026-11-08 (+08:00).
This PR contains only the plan; it authorizes no implementation, release,
service restart or data changes. Source inventory baseline: `15c5a8533`.

## Decisions

1. **A: ship a lean compatibility epoch.** Classify new migrations mechanically;
   after a bridge release, only breaking batches take a verified snapshot.
2. **B: defer background index jobs.** Removing the backup addresses the main
   observed cost; a roughly 31 s index upgrade does not justify that subsystem.
3. **C-alt: stop redundant Vibey tool-call trace copies.** Derive tool-step
   presentation from canonical context. Keep other backends' traces and use the
   separate single-copy-text change for them. A separate trace database is not
   selected now.

Keep [PR #3365](https://github.com/avibe-bot/avibe/pull/3365) independent: its
`docs/plans/migration-startup-performance.md` owns clone/reflink snapshots and
migration-aware readiness. This plan neither duplicates that implementation nor
claims cloning removes verification I/O.

Invariants: old supported readers/writers remain correct; incompatible state is
refused before migration side effects; canonical messages/context and external
effects are neither lost nor replayed; an omitted trace does not mean an omitted
UI step. **Retention defaults, messages retention, canonical-result retention,
Vault/avault boundaries and remote operations are unchanged.**

## Evidence

The reported `3.2.5rc18` upgrade used a 4.5 GB database on a 91%-used APFS volume:
the **whole backup stage** took about 111 s, migrations 0073–0075 about 31 s, and
downtime about 2.5 min. The ordinary 120 s supervisor wait false-failed. Another
reported `3.2.4` database is 6.1 GB. These are field observations, not this plan's
benchmarks; the historical 111 s cannot be separated into copy, wait,
verification and filesystem publication after the fact.

Reported table sizes: `agent_events` 1.7 GB, `messages` 1.5 GB, `agent_runs`
351 MB. `agent_events` is **not just disposable traces**: it also contains
canonical tool results/checkpoints, audit, Skill and other event classes.
Reported daily volumes are 11–31 MB tool-call traces versus 22–66 MB context
tool results. Attribute trace bytes to Vibey and remeasure after single-copy
storage before estimating savings; the canonical-result growth remains.

PR #3365's isolated 3.27 GB APFS sample measured clone creation in milliseconds
but backup `quick_check` at 9.803 s (~3 s/GB). Paired total-backup samples did not
show a material speedup. COW avoids eager allocation, not later divergence;
non-COW filesystems still copy. A removes the need for that snapshot on proven
additive upgrades rather than optimizing its copying again.

## A. Lean compatibility epoch

### State, classification and bridge

Bridge implementation and the owner-defined capability release boundary are
tracked in [the bridge delivery contract](../maintenance/sqlite-compatibility.md).
The bridge remains conservative; its presence alone does not enable no-copy
upgrades or C-alt trace suppression.

Store **one integer `compatibility_epoch` in core `state_meta`**. Each binary
knows which epochs it supports. Keep Alembic's stamp/graph for migration order;
no applied-ancestry ledger, schema-use lease or new runtime coordination system.

Each new migration declares `additive` or `breaking`; missing/unknown defaults
to **breaking**. CI derives the permitted class from a small operation allowlist,
not the author's label or historical `MIGRATION_SAFETY`:

| Allowed additive operation | Required condition |
| --- | --- |
| Create non-unique index | No new uniqueness/constraint, unsupported expression/function, or changed accepted writes |
| Create independent table | Does not constrain or change existing readers/writers through triggers or cross-table restrictions |
| Add nullable/defaulted column | Old named-column writes remain valid; a NOT NULL field has a compatible non-null server default; no new constraints |

Everything else is breaking: data rewrites/backfills, rebuild/drop/rename,
constraint/trigger changes, opaque SQL/helpers, unclassified repair/import work,
or changed meaning of existing persisted values. A new persisted representation
that older code cannot interpret must declare a breaking epoch transition even
if it needs no new column. A mixed pending batch takes the strongest class.

CI requires literal classification, checks emitted operations against the
allowlist, rejects an additive declaration it cannot prove mechanically, and
preserves the existing release-graph guard. Run the **immediately previous
release's** real reader/writer paths against new-version rows and reopen with the
new runtime. Cover omitted defaults, enum/JSON interpretation, unknown fields,
`INSERT OR REPLACE` and positional/`SELECT *` consumers where present. This is
one previous-release contract test, not an all-version matrix; semantic changes
still require review and a breaking declaration rather than a false CI proof.

Bridge: **R0 is the first release of any kind, RC or stable, implementing epoch
protocol v1**. A prerelease establishes the bridge for each DB it opens; the
per-DB marker, not a semver tag, is the authority. **R1 is a later release with
no-copy activation**. As clarified by the owner on 2026-11-08, it requires all
four: a valid marker already present at upgrade start, the immediately previous
published binary's verified protocol and reader/writer contract, an exact known
Alembic forward path, and an entirely proven-additive pending batch. A marker
first written during this upgrade does not qualify. This is capability evidence
for the released predecessor, not forensic identity of the last writer; do not
add a generation field or per-database lineage record.
Neither `3.2.4` nor `3.2.5rc18` understands the protocol. R0 uses the current
conservative upgrade/backup path and writes epoch 1 after reaching its required
schema. Direct upgrades from pre-marker versions remain conservative, as do
upgrades without verified predecessor evidence. Do not relabel or edit released
migrations to opt them in; fresh empty databases need no backup. Rolling back to
a pre-protocol binary requires the bridge-time backup; once it expires, rollback
to a pre-bridge binary is unsupported.

### Open and upgrade rules

All openers—controller, Web, CLI, explicit stores, bootstrap and readiness—share
one preflight decision before backup, repair or import:

- **Known forward graph:** use exact Alembic identity/reachability, then classify
  the pending batch. Same-head normal opens remain cheap.
- **Newer unknown exact revision, valid supported epoch:** allow an explicit
  **compatible-open**, not an Alembic upgrade. Check the old runtime's required
  schema; do not restamp, repair, import, rebuild indexes or replay old data
  migrations. Older supported application code may then read/write normally.
- **Unknown/incompatible compatibility state:** refuse malformed/unsupported
  epoch, inconsistent required schema, symbolic/partial stamps, known unrelated
  branches or explicit ancestor migration targets. A pre-marker unknown revision
  still fails closed. Keep #3342's executable/version/path diagnostics and its
  no-side-effects-before-refusal rule.

The second case is the **only intentional relaxation of exact-revision refusal**
in [#3342's contract](sqlite-migration-preflight.md). A valid supported epoch is
what makes future state known-compatible; an unknown stamp by itself never
proves compatibility. The marker is a release contract, not proof against
manual schema tampering or arbitrary third-party forks.

For new migrations, **DDL/data work, Alembic stamp and resulting epoch commit
in one explicit transaction**. Additive batches retain the epoch; breaking
batches raise it. Crash/rollback leaves either the old committed state or the
new one, never a stamp/epoch ahead of its schema. Do not rely on implicit Python
DDL transactions; a migration requiring work outside this contract must be
redesigned rather than marked additive. Historical repair/import paths remain
conservative; do not promise to retrofit atomicity into all 82 revisions.

Run breaking upgrades with the managed service stopped, under the existing
migration lock. Ordinary writers do not take that lock: concurrent old/new
runtimes across a breaking upgrade are unsupported and must be quiesced by the
upgrade procedure. This plan adds no schema-use lease or distributed writer
admission mechanism.

### Backup, verification and downgrade

Only breaking batches take a **fresh snapshot plus its verification**, on every
attempt; never reuse a prior copy merely because its revision label matches.
Keep existing post-migration/import validation and exact-attempt readiness for
those attempts. Proven additive batches skip snapshot creation and its full-DB
verification/lifecycle checks; validate the committed required schema, stamp and
epoch without adding a new full-file startup scan. Existing independent integrity
maintenance is not removed. Unclassified legacy repair remains breaking.

Within a supported epoch, rollback selects the older compatible binary without
Alembic downgrade or restore; new schema objects remain. Across epochs or to a
pre-marker binary, refuse and require an explicit verified restore with all
writers stopped, warning that it discards writes since the snapshot. Never
silently lower the epoch or restamp. Prefer forward repair.

Preserve [#1578](https://github.com/avibe-bot/avibe/pull/1578): rollback positions
are exact revision sets, keeping first/last attempts and recorded write order,
not epoch groups. Preserve [#2252](sqlite-backup-expiry.md): age expiry remains
72 hours after the real migration/import/readiness evidence; existing count
bounds and failed/restored/unconfirmed-copy protections remain. A no-copy
upgrade creates no fake receipt, adopts no prior attempt and resets no expiry
clock. Independently eligible older copies may still expire in maintenance.

## B. Deferred

Do not build an optional-index registry, background migrator or new readiness
state now. After A, future allowlisted index-only upgrades no longer back up;
the remaining reported 31 s for 0073–0075 does not justify that machinery.
Keep required schema/index creation before readiness and use #3365's truthful
migration-aware wait. Legacy pre-bridge upgrades still take their backup.

Reopen B if a **single measured index build exceeds a few minutes**, or measured
upgrade downtime remains unacceptable after A. `CREATE INDEX` holds SQLite's
writer for its transaction and cannot be chunked; WAL is not concurrent index
construction. The 5 s busy timeout and #3281's selective ~15.15 s retry ceiling
are not general writer protection. Any reopened design must also handle hard
`INDEXED BY` dependencies and synchronous index-repair paths; absence does not
always mean "only slower".

## C-alt: stop writing Vibey's redundant tool-call trace copies

This replaces the separate-trace-DB proposal. For Vibey, an assistant response
already commits call ID/name/arguments in
`messages.content_json.model.message`, with `context_seq`
(`storage/agent_transcript.py:append_response`).
`modules/agents/vibey/agent.py:_emit_tool_line` then renders the same call and
`core/message_mirror.py` inserts a `tool_call`/`trace` event. Remove only that
proven-redundant persistence, **not** tool execution or presentation.

The separate **trace-text single-copy lane** (`fix/trace-text-single-copy`)
owns storing rendered text once in newly written traces while preserving
`3.2.4` readers. Reference its eventual landed contract; do not duplicate it.
Claude/Codex/OpenCode lack equivalent Avibe-owned canonical context, so keep
their traces, using that single-copy representation. Select by persisted
response provenance/capability, not the Session's mutable current backend.
Existing rows are untouched; the existing retention policy drains eligible
legacy traces. No change to retained `tool_result`, checkpoints, audit, Skill,
prompt or `silent_terminal` events, and no bulk deletion or VACUUM.

### One projection for persisted and live tool steps

Today `storage/agent_activity_service.py:_timeline` merges messages with trace
rows and ignores tool-only responses with empty display text.
`core/message_mirror.py:_publish_activity_event` emits the inserted trace as a
synthetic `message.new`; `ui/src/lib/agentActivity.ts` and
`AgentActivityGroup.tsx` consume its step identity/order, hosted card and Turn
anchor. Skipping the INSERT alone would hide steps live and after reload.

Replace that dependence with one server-owned projection:

- Derive identity from **response row + call ID**, order from canonical
  block/context order, and anchors from the existing Turn attribution. Use the
  same bounded window for summary, lazy expansion and live/reload deduplication;
  supply explicit ordering instead of assuming every ID has an `evt_` clock.
- Reuse tool formatting/redaction and `core/hosted_activity.py`. Ordinary
  name/arguments come from the response; native hosted calls use its records;
  proxied hosted cards/results join retained tool-result context by **call
  instance**, including retry/fork boundaries. Preserve canonical media
  registration/authorization rather than depending solely on `_hosted_card`'s
  trace write for usable image URLs.
- Response blocks prove a call was **requested**, not executed. Runtime
  `ToolStarted` supplies live running state; committed results establish settled
  outcomes. Show pending/unknown/interrupted when recovery lacks start evidence;
  do not invent start times from response timestamps. Preserve only compact
  call-instance display metadata if exact timing/status is required but cannot
  be derived—never another copy of rendered arguments.
- Publish projected steps after their canonical source is durable, preserving
  `show_agent_activity`, suppression and authorization. Keep Session activity
  ranking and necessary media work when suppressing the trace insert. No tool,
  model or network operation is replayed to reconstruct presentation.

Ship projection-aware readers **before** stopping trace writes. Mark newly
committed responses with a versioned projection capability and link transitional
trace rows to their response/call identity. The reader-first release renders
these responses and still writes compatibility traces, deduplicating by identity.
Unmarked history stays on the legacy trace path; never fuzzy-match repeated
arguments. The next release suppresses traces only for supported, marked,
committed canonical responses; otherwise retain a real trace. The per-response
boundary supports mixed old/new rows in one Session without a backfill.

### Other readers and downgrade

| Reader | Contract |
| --- | --- |
| Web transcript/Activity/live publish | Same projected step IDs, counts, order and Turn grouping after reload; chat text is not replaced by tool arguments |
| Digests/context/recovery/forks | Digests derive from checkpoints and canonical messages/results; leave them, `context_seq` and `silent_terminal` unchanged |
| Message search | `messages_service.search_messages` searches message `content_text`, not trace payloads; no promised new tool-argument full-text search |
| `vibe data query` / saved trace SQL | Raw `agent_events` remains physical, so omitted rows are absent. Provide/document a read-only `tool_calls` projection over canonical Vibey and legacy/non-Vibey traces with source identity and derivable status; preserve ACLs. `transcript`/`turns`/`digests` retain their meanings |
| IM mirror | Keep formatting, mute/display decisions and delivery; suppress only redundant persistence, not ToolActivity output |
| Retention/metrics/backup | Legacy/non-Vibey eligible traces retain current policy; physical trace counts fall. Canonical-result retention, audit/cache facts and backup policy do not change |

A trace-only old binary, including `3.2.4`, would show newer chat responses but
miss tool steps after suppression. Canonical data alone does **not** preserve
that UI. The reader-first release must therefore be the supported rollback
floor: it understands the projection's epoch before activation, and the
write-suppression release records that minimum epoch under A if earlier allowed
readers lack the projection. Older trace-only binaries then refuse rather than
silently showing incomplete activity. The immediately previous projection-aware
release must render new rows correctly. Before that floor exists, keep writing
compatibility traces. There is no automatic backfill/export for unsupported
old-reader downgrades; an explicitly approved restore has A's data-loss warning.

The requested call's name/arguments are redundant; execution timing and some
hosted display metadata may not be. That distinction, plus reader/live ordering
and downgrade behavior, is the main implementation risk—not moving SQLite data.
Measure savings against the single-copy baseline and report untouched
22–66 MB/day context-result growth separately. This is less work than C's second
DB/routing/FK-reconciliation/file-set recovery design and removes redundant
Vibey inserts at source, but does not isolate non-Vibey trace writers or reclaim
the existing 1.7 GB immediately.

## Separate traces.sqlite: not now

Revisit only if measurement shows material **core-writer contention from traces**
after A, C-alt and single-copy storage. The earlier design would move only
`tool_call` + `visibility='trace'` + null `context_seq`, keep canonical history
core-resident, and require cross-file reconciliation and paired restore (WAL
ATTACH writes are not cross-file atomic). On a nearly full disk, prefer new-write
routing plus ordinary legacy retention: copy/delete does not shrink a NONE
auto-vacuum core file, and a rebuild needs headroom. These costs are not justified
without measured contention; no split-store implementation is planned now.

## Sequence, effort and validation

| Work | Dependency / boundary | Engineering effort |
| --- | --- | --- |
| #3365 snapshot/readiness | Independent existing lane | Owned elsewhere |
| A bridge + allowlist + previous-release test, then additive no-copy | Coordinate #3342/#1578/#2252; bridge must ship first | **About 1 week (5 working days)**, plus release cadence |
| Single-copy trace text | Independent lane; no change to existing rows | Owned elsewhere |
| C-alt reader/live projection, then write suppression + rollback floor | Reader-first release; A epoch gate; test against single-copy baseline | **4–7 working days**, plus reader adoption/release cadence |
| B / separate traces.sqlite | Only on the measured triggers above | Deferred; no current implementation estimate |

Current selected scope: **about 2–3 engineering weeks**, not a release-date
promise. No parallel consuming implementation starts before its owning contract
lands. Implementation PRs update relevant public English/Chinese upgrade docs.

Use isolated synthetic 4.5 GB and 6.1 GB databases with representative payloads,
indexes and distinct trace/context/audit cohorts. Record SQLite/Python versions,
allocated versus logical size, free bytes, cold/warm cache and repeated paired
runs. Use bounded test volumes, never fill the owner's disk or copy personal data.

| Gate | Evidence required |
| --- | --- |
| Baseline / A latency | Stop-to-true-readiness; separately time snapshot wait/copy/clone/quick_check/fsync, each migration and live validation. Additive path creates zero backups/backup receipts; breaking keeps fresh verified copies. Test APFS/reflink and non-COW paths |
| A compatibility/crash | Previous-release old writes against new rows; fresh/pre-marker/bridge/same-epoch/incompatible-epoch opens and all entry points. Crash before/after transaction commit must preserve matching schema/stamp/epoch. Refusal must precede backup/repair/import |
| A rollback/retention | Same-epoch binary rollback, cross-epoch/pre-marker refusal and explicit restore; mixed batches, failed attempts, first/last positions, exact 72-hour eligibility and no receipt adoption by additive upgrades |
| C-alt behavior | Tool-only responses, parallel/repeated calls, retries/forks reusing call IDs, cancellation/recovery, native/proxy hosted media/errors, mixed legacy/new rows, IM gates, ACLs, live versus reloaded step identity/order/status; trace-only old binary refused and previous projection-aware binary correct |
| C-alt benefit | Per-backend bytes/day, trace INSERT/index/WAL bytes, core-write p50/p95/p99 latency and retention work versus single-copy baseline. No lost steps or fabricated execution, no canonical-result deletion; do not claim immediate filesystem shrink |

## Appendix: complete 82-revision inventory

Read every upgrade and upgrade-reachable helper, including raw SQL and replayed
revisions; downgrade-only operations are excluded. Duplicate historical numeric
suffixes require **full date_revision IDs**. Files are under
`storage/alembic/versions/<ID>_*.py`.

Partition: **30 ordinary schema extensions + 20 non-unique-index-only + 2 new-key
uniqueness changes + 28 semantic/constraint/rewrite + 1 derived backfill + 1 no-op
= 82**, with no omissions/duplicates. The 50 ordinary schema/index candidates
support the "mostly additive" observation, but this structural inventory is
**not** permission to skip old backups or treat old declarations as A's allowlist.

| Upgrade shape | Count | Full revision IDs |
| --- | ---: | --- |
| New tables, sometimes indexes | 16 | `20260501_0001`, `20260515_0002`, `20260523_0004`, `20260526_0006`, `20260530_0009`, `20260602_0013`, `20260604_0016`, `20260608_0020`, `20260621_0023`, `20260707_0029`, `20260725_0035`, `20260725_0036`, `20260725_0037`, `20260725_0038`, `20260907_0061`, `20261020_0071` |
| Nullable columns, sometimes indexes | 10 | `20260603_0014`, `20260604_0015`, `20260604_0017`, `20260610_0022`, `20260705_0028`, `20260726_0036`, `20260731_0043`, `20260812_0053`, `20260923_0062`, `20261013_0065` |
| NOT NULL columns with compatible server defaults, no explicit rewrite | 4 | `20260525_0005`, `20260601_0012`, `20260627_0025`, `20260724_0034` |
| Non-unique-index-only, including replacements | 20 | `20260606_0018`, `20260606_0019`, `20260622_0023`, `20260721_0031`, `20260723_0033`, `20260727_0038`, `20260728_0039`, `20260728_0040`, `20260728_0041`, `20260729_0042`, `20260808_0048`, `20260821_0059`, `20261012_0064`, `20261015_0066`, `20261016_0067`, `20261016_0068`, `20261018_0070`, `20261107_0073`, `20261107_0074`, `20261107_0075` |
| Nullable identities plus correctness-critical UNIQUE indexes | 2 | `20260811_0051`, `20261002_0063` |
| Derived new-table backfill (session digests) | 1 | `20261030_0072` |
| No-op graph merge | 1 | `20260804_0047` |

The remaining **28** require conservative treatment:

| Revision | Upgrade work |
| --- | --- |
| `20260522_0003` | Legacy table/column renames and message/session-policy backfills |
| `20260529_0007` | Backfill routing from JSON |
| `20260530_0008` | Retarget references/default and delete legacy Agent |
| `20260531_0009` | Defaulted type column **and** classify existing messages |
| `20260531_0010` | Nullable source column **and** provenance backfill |
| `20260601_0011` | Normalize anchors, move transcripts, dedupe Sessions, add uniqueness |
| `20260608_0021` | Detach references and delete tool-call messages |
| `20260626_0024` | Remove values from existing JSON |
| `20260703_0026` | Vault data conversion, table drops/rebuilds |
| `20260704_0027` | Folded-name uniqueness and rejection triggers, not a performance index |
| `20260716_0030` | Rewrite harness message author/type before replacing indexes |
| `20260723_0032` | Rebuild nullability; rewrite Session anchors/scope/visibility |
| `20260726_0035` | Add dispatch state and mark historical events accepted |
| `20260726_0037` | Reconcile Show message identity; drop dispatch state |
| `20260801_0044` | Delivery/turn tables plus message movement/deletion and FK/check rebuilds |
| `20260802_0045` | Rebuild message uniqueness and change delivery dedupe keys |
| `20260804_0046` | Canonicalize message timestamps before index replacement |
| `20260806_0047` | Rebuild Sessions to remove queue-hold columns/check |
| `20260809_0049` | Rewrite notify to vault messages plus indexes |
| `20260811_0050` | Rebuild delivery receipt check |
| `20260811_0052` | Pause orphan-owned Tasks and rewrite metadata/timestamps |
| `20260815_0054` | New fields plus nullability rebuild and scoped uniqueness |
| `20260817_0055` | Visibility/access-mode conversion, backfills/checks/rebuild |
| `20260819_0056` | Replay five upgrades, including 0054's rebuild |
| `20260819_0057` | Rebuild deliveries to repair JSON server default |
| `20260820_0058` | Copy audiences to replacement table, drop old table |
| `20260821_0060` | Rewrite event timestamps; fetch batching is not transaction chunking |
| `20261016_0069` | Direct writable_schema/nullability surgery plus index repair |

0051/0063 leave old keys NULL but introduce correctness-critical uniqueness;
0072 preserves source rows but scans history for its derived backfill. 0069's
existing `MIGRATION_SAFETY='additive'` is not the proposed mechanical guarantee.
Of the 20 index-only revisions, 0064 has hard `INDEXED BY` users in
`storage/agent_transcript.py`; 0073–0075 are structurally index-only, but no such
classification removes their write locks. 0040 is superseded by 0041. Repair
paths in `storage/migrations.py` also create/rebuild indexes outside Alembic;
all startup mutation belongs in A's preflight/classification audit.

Primary SQLite references: [transactions](https://www.sqlite.org/lang_transaction.html)
(one writer), [ATTACH](https://www.sqlite.org/lang_attach.html) (WAL atomicity is
per file), [auto-vacuum](https://www.sqlite.org/pragma.html#pragma_auto_vacuum)
(NONE does not shrink on deletion), and [VACUUM](https://www.sqlite.org/lang_vacuum.html)
(rebuild/temporary-space requirements).
