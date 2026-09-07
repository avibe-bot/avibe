# Harness Skill Observability

Status: Implemented in an isolated development worktree; not deployed.
Date: 2026-09-07.
Design baseline: `e8060055d291fa1f4f0897b4a8947b1a5854c1ee`.
Implementation migration: `20260907_0061` (parent `20260821_0060`).
Schema contract: [harness-skill-observability.sql](harness-skill-observability.sql).

## Decision

Add **one table**, `skill_usage_daily`, for session-grained daily Skill metrics.
Reuse `agent_events` for structured observations, adding one partial index and
two event types without adding columns. Keep operational Sessions, Turns, Runs,
deliveries, and Model Hub usage under their existing owners.

The first release answers which Skills Avibe offered and successfully loaded,
how broadly they were loaded, and where loading failed. These are observable
facts, not evidence that a Skill was followed or caused task success.

For example, one Session loads the same Skill twice on Monday and once on
Tuesday. The report shows three loads, two active days, and **one** distinct
Session. Adding two daily distinct-Session counts would incorrectly report two.
Keeping Session identity in the aggregate preserves the correct answer after
raw-event retention expires.

This design adds local statistics only. It does not create a dashboard, change
Skill ranking or instructions, add agent self-reporting commands, run task-content
classification, or upload data. No cloud table or telemetry vendor is required.

## Baseline Sources and Gaps

| Existing owner | Reuse | Boundary |
| --- | --- | --- |
| `vibe/cli.py:cmd_skill` | Explicit list/load operation boundary | Currently resolves, reads, and prints; it does not record Skill observations. |
| `core/managed_skills.py` | Actual winning Skill, catalog pagination, loaded body | Discovery reads frontmatter, not every body. Ordinary filesystem reads can bypass the command. |
| `storage/agent_events_service.py` | Internal events with Session/Turn/Run references | Existing tool events often contain presentation text, not structured tool results. |
| `core/caller_context.py` | Session, backend, optional Run and origin | No uniform exact Turn identity; a long-lived Claude environment cannot carry changing authors or Turns reliably. |
| `session_turns`, `agent_runs`, `message_deliveries` | Operational state, timestamps, parent/child links, receipts | A completed Turn is not independently verified task success. Not all IM turns have a durable Workbench Turn. |
| Model Hub usage and provenance | Existing token normalization and routing evidence | Gateway observations are partial coverage, not a complete ledger of all backend activity. |

Existing tool traces expire under a separate policy, currently defaulting to
30 days. The existing retention predicate only deletes `tool_call` trace rows.
Adding Skill event types alone would therefore leave them indefinitely retained;
implementation must add an explicit Skill policy to the existing retention owner.

## Observation Contract

### Common envelope

Use existing `agent_events` columns as follows:

| Column | Meaning for Skill observations |
| --- | --- |
| `id` | Producer-allocated immutable observation ID; reused only for retransmission. |
| `scope_id`, `session_id` | Validated local execution ownership; nullable. |
| `turn_id`, `run_id` | Exact, independently validated links when available; otherwise null. |
| `platform`, `backend`, `agent_name` | Execution snapshot; unknown values stay unknown. |
| `event_type` | `skill.catalog_result` or `skill.load_result`. |
| `visibility`, `source` | `trace`, `runtime`; never transcript or Inbox content. |
| `content_text` | Null; no copied Skill text, shell command, or free-form error. |
| `content_json` | Typed event payload below. |
| `metadata_json` | Observation schema and execution provenance below. |
| `created_at`, `updated_at` | Existing recorder timestamps, not the producer's clock. |
| `sequence` | Null unless supplied by an existing authoritative execution sequence. |

Common `metadata_json` fields:

```json
{
  "schema_version": 1,
  "observed_at": "2026-09-07T03:00:00.000000Z",
  "observation_channel": "avibe_cli",
  "correlation_level": "session_only",
  "trigger_kind": "human",
  "model": null,
  "avibe_version": "fixture-release",
  "context_ref": null
}
```

`observation_channel` is `avibe_cli` or `runtime_prompt`.
`correlation_level` is `exact_turn`, `session_only`, or `unattributed`.
`context_ref` is an optional execution-context reference from the runtime, not
an agent-generated description. Record the model only from evidence applicable
to this execution; do not read the Session's current model later and backfill it.
The trigger taxonomy is declared in the SQL contract; an unknown origin is not
automatically human. A direct terminal command is `standalone` only when that
origin is established, not merely because its Session reference is missing.

The IPC metadata also carries `backend`, which the writer moves into the
existing event column. It comes from the bound CLI environment or the backend
adapter, not a later Session configuration lookup. Scope, platform, and agent
label are resolved from validated Session ownership at receipt time; they are
not independently observed historical execution facts.

### Catalog result

Emit `skill.catalog_result` when a `vibe skill list` result has been written to
stdout, or when a catalog-bearing prompt has positive backend acceptance. Also
record failed list operations with a bounded reason code and an empty entry list.
Pure resolver scans, preview pages, and prompt rendering are not offers.

Payload fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `entry_point` | string | `cli_list` or `runtime_prompt`. |
| `outcome` | string | `success` or `failure`. |
| `error_code` | string/null | Stable reason, such as `invalid_page` or `output_interrupted`. |
| `page` | integer | Actual returned catalog page; first prompt page is 1. |
| `has_more` | boolean | Whether another page was available at the observation. |
| `catalog_digest` | string/null | SHA-256 of this exact ordered page descriptor, including descriptor versions. |
| `entries` | array | Actual returned entries, each with `skill_key`, `skill_name`, `source_kind`, `descriptor_sha256`, and 1-based page position. |

Each entry increments `catalog_offer_count` once in a bucket whose
`skill_revision` is empty. A body revision is not available during discovery;
do not read all Skill bodies merely to assign one. The descriptor digest covers
the normalized name, description, and model-invocation setting. Store the digest,
not the description. Empty or failed pages contribute no per-Skill buckets.

An accepted prompt is an offer to the backend, not proof of model attention.
Reusing an unchanged prompt in a persistent backend does not emit another offer
unless there is evidence of another catalog submission. Replaying the same
acceptance receipt does not create another offer. An actual new submission gets
a new observation ID even when the catalog digest is unchanged.

Do not label loads divided by offers a selection probability: repeated loads,
retained context, direct user requests, and incomplete prompt-acceptance coverage
make the populations different. Page/position analysis is diagnostic only until
an evaluation defines a common exposure and selection unit.

### Load result

Emit `skill.load_result` once after a load operation finishes, including failure.
Success means the selected body was read and output completed, including flush;
it does not assert that downstream tools avoided truncation or the model read it.

| Field | Type | Meaning |
| --- | --- | --- |
| `skill_key` | string/null | Resolved local identity, or unresolved lookup identity. |
| `skill_name` | string/null | Validated portable Skill name. Invalid arbitrary input is not persisted. |
| `source_kind` | string | `builtin`, `project`, `global`, or `unresolved`. |
| `skill_revision` | string/null | Digest of the successfully read instruction snapshot; may exist even when output later fails. |
| `descriptor_sha256` | string/null | Descriptor version from that same read. |
| `outcome` | string | `success` or `failure`. |
| `error_code` | string/null | `invalid_name`, `not_found`, `unreadable_or_invalid`, `output_interrupted`, or `internal_error`. |
| `duration_ms` | integer/null | Monotonic duration from resolution start through output completion, excluding observation persistence. |
| `body_bytes` | integer/null | UTF-8 byte length of the loaded body, excluding the wrapper; not a token estimate. |

Preserve the existing public CLI output contract. Internal error classification
may become more precise later, but a loader returning `None` only justifies
`unreadable_or_invalid`, not an invented parse or permission diagnosis.
An invalid name still produces a failure event without a Skill bucket.
Successful loads require a nonempty identity and revision. Failed loads increment
failure counters; only successful loads contribute `loaded_body_bytes_sum`.

### Skill identity and revision

`skill_key` is SHA-256 over a versioned canonical JSON tuple:

- Built-in: `[1, "builtin", declared_name]`; published snapshot directories do
  not change the logical identity on every Avibe upgrade.
- Resolved user Skill: `[1, source_kind, canonical_directory, declared_name]`.
  Symlink aliases resolving to the same winning directory share identity;
  different project installations remain distinct.
- Valid but unresolved lookup: `[1, "unresolved", canonical_project_base_or_cwd,
  requested_name]`. It cannot be confused with a resolved Skill.

Canonical JSON uses UTF-8, ordered tuple elements, no optional whitespace, and no
ASCII escaping. Paths are inputs to the local key, not persisted fields. These
hashes are local correlation identifiers, **not anonymous or globally portable
identities**. A moved directory becomes a new identity; v1 has no identity-merger.
Plugin locations use their effective global/project scope, not another registry.

`skill_revision` hashes the canonical tuple `[1, name, description,
disable_model_invocation, body]` from the same successful read. It identifies the
observed instruction snapshot, not the entire package: supporting scripts,
references, and assets are excluded. Full package manifests belong in a later
reproducible-evaluation contract if required. No new frontmatter is required.

## Daily Table

The SQL file is the exact proposed DDL. One row has this grain:

```text
UTC day x scope x Session x Skill identity x instruction revision
        x backend x model x trigger kind x platform x Avibe version
```

| Fields | Responsibility |
| --- | --- |
| `id` | Local surrogate primary key. |
| `day` | Validated UTC calendar date. Query windows use half-open UTC dates. |
| `scope_id`, `session_id` | Nullable local ownership with cascade deletion. |
| `skill_key`, `skill_name`, `source_kind`, `skill_revision` | Immutable observed Skill dimensions. |
| `backend`, `model`, `trigger_kind`, `platform`, `avibe_version` | Runtime snapshot dimensions, not joins to today's settings. |
| `catalog_offer_count` | Number of recorded successful offers of this Skill. |
| `load_success_count`, `load_failure_count` | Number of terminal load observations by result. |
| `load_duration_samples`, `load_duration_ms_sum`, `load_duration_ms_max` | Sample count, sum, and maximum for load durations across both outcomes. |
| `loaded_body_bytes_sum` | Body bytes from successful loads only. |
| `first_observed_at`, `last_observed_at` | Earliest/latest observation contributing to this bucket. |

Use empty strings for an unknown revision/backend/model in bucket keys, while
the raw event uses JSON null. Normalize optional scope/Session IDs with
`coalesce` in the unique index: SQLite's ordinary NULL uniqueness would otherwise
allow duplicate standalone buckets. No fabricated Session is created.

The writer validates ISO dates, fixed-width UTC microsecond timestamps, bounded
strings, integer counters, and consistent identity descriptors before insertion.
Check constraints defend the stored shape; they do not replace input validation.
Catalog offers have unknown instruction revisions and must be grouped across
revisions to compare with loads of the same logical Skill.

No lifetime counter is added to a Skill registry: a registry entry is mutable,
and removal/reinstallation must not redefine historical observations.

## Recording, Correlation, and Reliability

Use one shared storage operation to insert the event and update its daily
projection in the **same SQLite transaction**. The existing generic append
function currently allocates a fresh ID; the Skill recording operation must
accept the producer's immutable observation ID explicitly.

1. Resolve and validate ownership and the event schema.
2. Insert with `ON CONFLICT (id) DO NOTHING`.
3. Only if a new event was inserted, upsert its daily contributions.
4. Commit both, then mark the observation accepted. Any error rolls back both.

HTTP 202 with `status=queued` acknowledges bounded queue admission only, not a
durable commit. The runtime health counters distinguish queued, accepted,
duplicate, rejected, disabled, and dropped observations. There is no retry spool.

A duplicate ID with a different semantic payload is a contract violation, not
another observation. Do not use a broad ignore that masks other constraints.
Actual repeated CLI invocations allocate new IDs and therefore remain visible.

CLI producers submit through bounded internal IPC to the existing local service;
the catalog-prompt producer uses the same recorder in the runtime. The CLI's
resolved metadata describes the file it actually read. Do not re-resolve in the
server's working directory. Use the existing trusted local-caller boundary;
Skill names, environment strings, or an agent-supplied Session ID alone are not
authorization to inspect another user's observations.

Session linkage is useful even without a Turn. Exact Turn/Run attribution
requires execution-scoped evidence tied to the originating request. In particular,
do not pick the Session's currently active Turn at receipt time: delayed tools,
background children, and persistent backend processes can belong to earlier work.
Stale or unverifiable Run/model/trigger fields remain unknown. Native backend
subagents inheriting a parent environment are not independently attributed unless
the runtime supplies a distinct execution identity. Cross-backend and Web/IM
coverage must be reported separately until those paths have verified parity.

Statistics must not turn an otherwise successful Skill load into a failure.
IPC and storage waits have finite budgets; a full recorder queue or unavailable
service drops the observation with a bounded diagnostic and producer-side
health counter. Do not add an offline spool or retry indefinitely in v1.
If transport retries an unacknowledged submission within its budget, it reuses
the ID. Reject submissions outside a 24-hour observation-age bound so deleted
dedupe evidence cannot be reintroduced after retention. Validate future clock
skew as well; rejected observations count as collection failures.

Diagnostic counters cover accepted, rejected, dropped, and uncorrelated
observations and expose their process start/reset time. They are best-effort:
an abrupt producer crash can lose both an event and its counter. Reports state
collection start, retention limits, unknown attribution, and observed gaps;
they never claim complete usage coverage. Existing `state_meta` may hold the
first-accepted-observation timestamp and the clear-history watermark described below; it is
not another metric-series store.

No browser SSE subscription is used as a recording source. UI availability and
display settings cannot decide whether an operation contributes to statistics.

## Queries and Guarantees

Example: successful-load ranking for the 30 complete UTC days before September 8.

```sql
SELECT skill_key, skill_name, source_kind,
       SUM(load_success_count) AS successful_loads,
       COUNT(DISTINCT CASE WHEN load_success_count > 0
                           THEN session_id END) AS distinct_sessions,
       COUNT(DISTINCT CASE WHEN load_success_count > 0
                           THEN day END) AS active_days,
       SUM(CASE WHEN session_id IS NULL
                THEN load_success_count ELSE 0 END) AS unassociated_loads
FROM skill_usage_daily
WHERE day >= '2026-08-09' AND day < '2026-09-08'
GROUP BY skill_key, skill_name, source_kind
HAVING SUM(load_success_count) > 0
ORDER BY distinct_sessions DESC, successful_loads DESC, skill_key;
```

- Distinct Sessions are computed across the whole requested window, all
  revisions, and all selected runtime dimensions. Never sum distinct counts
  from days, backends, or Skills. Unassociated loads do not invent a user.
- Built-in versus non-built-in is an available filter; being built-in does not
  imply a Skill is only operational guidance.
- Load failure rate is failures / (successes + failures); a zero denominator
  is undefined. Invalid-name failures are available separately from raw events.
- Average duration is sum / sample count. Daily maxima can be combined with
  `MAX`; P50/P95 cannot be reconstructed from this summary and require retained
  raw observations. Byte counts are not token or money measurements.
- Same-Turn reloads, error classifications, catalog page/position comparisons,
  and exact execution joins use raw events within retention. Same-Session loads
  on different days are not automatically redundant work.
- Access uses the existing scope/Session authorization boundary. Instance-wide
  reports and unassociated observations require local-owner/admin access.
  Querying statistics must not bypass Skill or project authorization.

Internal queries are sufficient for v1. An implementation may extend the
existing read-only data-query catalog with the same access controls; it must not
make a global statistics endpoint implicitly available to every remote caller.

## Retention and Deletion

- Raw Skill events: 90 days by recorder `created_at`; own explicit event-type
  allowlist within the existing retention service. Tool and silent-terminal
  policies stay independent.
- Daily rows: 365 UTC dates including today; prune in bounded batches by `day`.
  This is a session-grained local history, not an anonymous global aggregate.
- Daily rows are updated transactionally at recording time, so raw pruning
  cannot erase pending aggregation work. Do not rebuild older daily rows from
  a partially retained raw window or reset them on upgrade.
- Purging a Session deletes its Skill events before the existing event FK can
  set `session_id` to null; daily rows cascade. Purging a scope removes both.
  Archiving is not purging. Restore/replay must not resurrect purged events.
  Late submissions referencing deleted ownership are rejected, not reassigned
  to an unassociated bucket.
- Clearing statistics deletes only these event types and their daily rows. In
  the same transaction, set `state_meta` key
  `skill_observability.cleared_through` to the current UTC timestamp. The recorder
  rejects observations at or before that watermark, including retried old IDs;
  validate observation time against the receiving local clock so a future-dated
  event cannot evade clearing. Recheck this watermark and collection enablement
  inside the recording transaction, after taking the writer lock. Disabling
  future collection and clearing history are separate actions.
- Time bounds limit history, not event rate. Queue and batch bounds protect the
  foreground path; monitor row growth before introducing cardinality eviction.
  Any future eviction must report reduced coverage rather than silently bias
  the ranking.

Alembic revision `20260907_0061` creates this table/index and updates model
metadata, with no historical backfill from free-form tool text. Downgrade removes
only the new objects and these event types. Existing data is unchanged by
upgrade and downgrade. DDL tolerates the existing head-schema repair/replay path.

## Further Harness Measurements

These remain a prioritized roadmap, not additional empty tables in this change.

| Priority | Measurement and producer | Decision supported |
| --- | --- | --- |
| Next | Queue/start/terminal and callback latency from delivery, Turn, and Run owners; distinguish start from useful first output | Locate scheduling, backend startup, and delivery delays. |
| Next | Structured tool completion, stable tool category, error class, retry/recovery links from shared adapter events | Improve tool contracts and recovery without parsing shell text. |
| Next | Actual model and normalized reported usage from existing Model Hub/backend owners, with source and coverage | Compare resource use without double-counting gateway and SDK reports. |
| Later | Actual prompt-injection bytes, compaction boundaries, Memory retrieval latency/result counts | Improve context supply; a retrieval hit does not establish usefulness. |
| Later | Task/Watch firing delay, no-event cycles, parent/child Runs, callback accepted versus processed | Reduce empty wakeups and lost or delayed follow-ups. |
| Later | Explicit user corrections, stop/steer/approval events and verified task outcomes | Measure human intervention and outcome quality separately. |

Use existing operational rows for facts they already own. New observations can
use typed internal events when needed; do not create another Turn state machine,
token ledger, task registry, or generic metric-name/value table preemptively.
Per-task outcome identity and its evaluator need a separate contract before
claiming task success rate. Agent self-report, process exit, test evidence, and
human acceptance are different evidence types. Multiple Skills in one task do
not each receive the task's entire cost or causal credit.

Build a versioned, consented, sanitized evaluation set from representative
failures. Compare the same cases across Harness changes; field usage is a
diagnostic signal and cannot itself establish causal improvement.

## Local Collection and Future Fleet Analysis

Local metadata collection uses `runtime.skill_observability_enabled`, defaulting
to `true`. Set it to `false` in the existing runtime configuration to stop new
collection; the writer rereads it before each transaction commits. Invalid or
unreadable configuration fails closed. It stores no new Skill bodies,
conversation text, tool arguments, filesystem paths, credentials, or raw errors.
Names and locally correlatable hashes are still private local data.

Fleet analysis is a separate opt-in capability and is off in this design.
Before implementing it, define consent, upload/delete behavior, and server
retention. Public Skills require verified registry/source identities, not names
or unverified local hashes. Private Skills contribute only explicitly permitted
coarse categories. Do not upload Session IDs, paths, names of private Skills, or
body hashes. Deduplicated installations are not deduplicated people, and daily
installation counts cannot be summed into monthly unique installations. A cloud
schema must be designed against that future consent and identity contract.

## Operations and Coverage

- `vibe data skill-usage --json` reports enablement, first accepted observation,
  clear watermark, row counts, and retention windows. It is not a usage ranking UI.
- `vibe data query --sql 'SELECT ...' --json` runs the queries above.
- `vibe data skill-usage --clear --yes --json` clears only Skill statistics.
  Disabling collection does not clear history, and clearing does not disable it.
  This is logical deletion, not secure erasure of SQLite free pages, WAL, or
  pre-existing database backups. Explicitly restoring an older full database
  also restores its historical statistics and watermark; clear again afterward
  if that restored history must be removed.
- These statistics require Instance Owner access. Non-owner SQL requests cannot
  read either `skill_usage_daily` or `agent_events`, including through CTEs,
  because the generic SQL reader has no per-resource row filter.
- The owner-only internal Unix socket provides
  `GET /internal/skill-observations/health`. Counters reset at controller restart;
  an event dropped before reaching the controller cannot appear in its counters.
- CLI IPC uses a 250 ms timeout per transport phase. Payloads are capped at
  32 KiB, the queue at 128 observations, and there is one serial database writer.
  Shutdown drops queued optional work and joins the single in-flight write.
- The existing maintenance worker prunes up to ten 1,000-row batches per surface
  per pass. It never vacuums automatically and runs independently of collection
  and tool-trace retention enablement.
- Claude offers a catalog once after the new client's first accepted query.
  Codex counts positively acknowledged developer-item injections, not prompt
  rendering or unchanged cached instructions. OpenCode counts accepted initial
  prompts and matching catalog resubmissions during live steering/retries.
- Restored OpenCode polls have no trusted catalog candidate and are not counted
  retrospectively. Native subagents, direct filesystem reads, and ambiguous
  native acknowledgements are not claimed as complete coverage.
- Run/model linkage remains unknown in this release. Exact Turn linkage is used
  only when the runtime request names a matching durable `session_turns` row.
  CLI attribution remains Session-only even if an inherited shell names a Turn.

## Verification

The proposed DDL was executed in an in-memory SQLite database with minimal
fixtures for the three referenced existing tables. Seven SQL assertions passed:
cross-day/version/backend distinct counting, nullable-key upsert, aggregate
survival after raw deletion, duplicate observation handling, transactional
rollback, Session cascade deletion, and scope cascade deletion. Foreign-key
checking returned no violations.

Implementation tests additionally exercise real migration upgrade/downgrade and
replay, models/migration parity, concurrent duplicate receipts, atomic rollback,
nullable buckets, UTC retention boundaries, indexed pruning, narrow clear with
a watermark, physical Session purge versus archive, owner-only SQL, strict
configuration, bounded IPC/queue behavior, and backend acceptance boundaries.
The combined targeted suite passes 1,908 tests and 28 subtests (one platform
capability skip). This includes the installed Codex binary's three loopback
prompt-contract tests, release migration guards, unrelated settings saves that
preserve the collection opt-out, localized CLI errors/help, and recorder cleanup
on server exit, cancellation, or startup failure. The expanded suite also passes
with the CI-resolved FastAPI 0.141.1 and Starlette 1.6.0 versions. Ruff and
whitespace validation also pass.
No production database or runtime configuration was migrated or deployed.
Container regression is unverified: the workstation's Incus client has no
configured Linux daemon. No remote operational environment was substituted.

## Implementation Acceptance

- [x] CLI and accepted prompt boundaries produce factual, typed observations;
  scans/rendering alone do not change counts.
- [x] An observation changes the event log and aggregate together exactly once;
  retries, concurrency, and transaction rollback preserve that property.
- [x] Distinct-Session queries remain correct across dates, revisions, models,
  and missing Session linkage, including after raw-event pruning.
- [x] Unproven execution attribution stays unknown on every backend and platform.
- [x] Observation failure leaves the original Skill operation's output and exit
  semantics intact and exposes the observed collection gap.
- [x] Invalid inputs, missing bodies, and interrupted output cannot count as a
  successful load or introduce arbitrary command text into statistics.
- [x] Authorization, purge, disable, and retention apply consistently to raw and
  aggregate data; existing unrelated row shapes survive migration unchanged.
- [x] SQL models and migration agree on columns, constraints, and indexes. All
  verification uses test-owned databases, never the local running service.
