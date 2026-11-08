# Vibey canonical tool activity

Contract: `sqlite-upgrade-downtime.md`, section C-alt. Reader-first #3371 shipped
in `gh-v3.2.5rc19` with epoch support `{1, 2}` and compatibility trace writes.
Stage 2 activates epoch 2 and suppresses only proven-redundant committed calls;
the reader-first invariants below remain the presentation contract.

## Invariants

- Only newly committed, explicitly marked Vibey responses project calls. Backend
  changes on a Session never reinterpret its earlier messages. Unmarked history,
  unknown capabilities and other backends retain the existing trace reader.
- A call is identified by its response row and provider call id. Canonical block
  order is explicit; repeated arguments and reused ids in retries/forks never
  match heuristically. Transitional traces link to that identity and deduplicate.
- One projection supplies the activity summary/detail and live publication. The
  source commits before publication; no reconstruction executes tools or models.
- A request is not execution evidence. Only ToolStarted records a start; retained
  canonical results establish outcomes. Compact display metadata stores timing
  and source labels/state, never arguments, rendered tool text or result bodies.
- Canonical media registration supplies authorized image URLs independently of
  trace persistence. IM formatting/delivery, activity visibility gates, Session
  ranking, canonical context, retention and audit remain unchanged.
- Reads are bounded by retained response ids and each response's context interval;
  live work seeks one response. SQL diagnostics gain a read-only `tool_calls` view
  without changing the physical `agent_events` table or its access policy.

## Additive wire contract

Stable step id: `tool:<response-row-id>:<URL-encoded-call-id>`.

Activity rows carry `order_micros`, `order_index`, `status`, `started_at`,
`ended_at`, `duration_ms`. Live `message.new` carries the same fields under
`content.activity`. Existing hosted cards remain `content.hosted` live and
`hosted` in activity rows. Status vocabulary: `pending`, `running`, `completed`,
`failed`, `interrupted`, `unknown`. The frontend lane owns consuming explicit
order and upserting same-id state changes. No chat arguments become chat text.

Native calls sort just before their response; ordinary calls just after it;
canonical block index orders calls at the same anchor. Group/Turn attribution
uses the existing activity boundary projection. The current 500-message /
2000-trace window applies equally to summary and expansion; live steps use the
same identity/order calculation.

## Diagnostics and rollout

`vibe data query` exposes a connection-local, read-only `tool_calls` view:
`session_id`, `source` (`canonical` or `trace`), `source_id`, `response_id`,
`call_id`, `context_seq`, `id`, `order_micros`, `order_index`, `name`,
`arguments_json`, `status`, `started_at`, `ended_at`, `duration_ms`, `turn_id`,
`text`. Legacy traces do not invent call ids, arguments or execution evidence.
For example:

```sh
vibe data query --sql "select id, source, name, status from tool_calls where session_id = 'ses...' order by order_micros, order_index, id"
```

It retains the existing `agent_events` Member/Owner diagnostic access requirement;
it is not a way around the raw-table authorizer. `agent_events` stays physical,
and `transcript`, `turns`, `digests` and message search keep their meanings.

### Reader-first history (shipped in RC19)

The additive UI consumer (#3369) and conservative compatibility bridge (#3370)
have landed first. The owner reserved **epoch 2 for C-alt's representation** on
2026-11-08; section A's lane owns the shared epoch registry. This reader supports
`{1, 2}` before activation, so it can compatible-open a future exact revision
holding projection-only calls. That release's bridge writes epoch 1. The reader
PR advanced no marker and added no migration. Its synthetic epoch-2 fixture proved
Activity/SQL reconstruction without traces, restamping, startup mutation, backup
creation or receipt adoption; it is not the later real previous-release test.

The reader-first release writes all compatibility traces, so trace-only
rollback remained correct at that stage. It added response capability/source links, compact start/finish/source-display facts,
and exact canonical image registration tokens; it does not change model payloads
or `context_seq`. The event consumer binds call ids at `MessageCommitted`, not in
the producer's ahead-of-consumer store callback, so a queued event cannot be
attributed to a later response that reused the id. Live recovery publishes its
committed settlements before terminal delivery or automatic continuation; a
missing ToolFinished cannot leave the last live step running after Stop. No
storage savings were claimed while the compatibility writer was still enabled.
Stage 2 below owns epoch activation, previous-release compatibility testing,
upgrade documentation and measurements against #3367's single-copy baseline.
Its **released projection-aware reader floor and explicit owner approval** now
exist; epoch-2 reader support alone was never permission to suppress writes.

## Stage 2: epoch-gated write suppression

Authorized after the RC19 reader release on 2026-11-08. Breaking revision
`20261108_0080` advances the database marker to `VIBEY_TOOL_PROJECTION_EPOCH = 2`
in the same transaction as its Alembic stamp. It changes no rows or tables beyond
the marker/stamp. A populated upgrade keeps the normal fresh rollback snapshot;
it is **not** an additive no-copy migration. Stop every writer before upgrading.

At the mirror persistence boundary, omit a `tool_call`/`trace` INSERT only when:

1. The exact response/call source link resolves to a supported, marked Vibey
   canonical response with a committed `context_seq` in this Session.
2. The projection supplies this exact step, in the same platform and scope as
   the persistence target (routed destinations otherwise retain their trace).
3. This transaction reads epoch 2 and the build's actual released-predecessor
   reader/writer proof enables epoch 2.

An unverified build, epoch 1, unmarked/unknown/non-Vibey response, malformed or
missing source, uncommitted row, wrong Session/call or routed scope retains a
real single-copy trace. No Session.current_backend inference is used. The
projected row still drives Session ranking and the same gated live publication.
IM output is not suppressed. Canonical media is already registered at commit;
the omitted legacy card write cannot mint a second capability. Results, existing
traces, retention, checkpoints, audit, and context sequencing are untouched.

### Released-reader proof

The existing isolated predecessor harness now writes actual candidate responses,
retained results and mirror output, asserts zero redundant physical calls, and
compares stable identity/order/outcome/Turn facts from the released binary's
projected steps, Activity summary/detail and `tool_calls` SQL to the candidate.
Renderer wording and additional display fields may differ without failing a
compatibility proof. Reused provider ids in distinct responses
retain distinct outcomes. Old named-column writes and new reopen still pass;
unsupported readers must refuse the **actual candidate revision**, not only a
synthetic unknown future stamp, without changing the DB or backup receipts.

On 2026-11-08 the actual wheels verified were:

- RC19, the rollback floor: SHA-256
  `e19c6b0bcfcbbdb6716842653c25c5231cf5d7600fd69fda0a3cb4f4bf2630be`.
  Epoch-2 projection and reader/writer contract passed. Reproduce with
  `python scripts/sqlite_predecessor_contract.py --release-tag gh-v3.2.5rc20`
  (the existing selector chooses the immediately preceding publication, RC19).
- RC20, the then-current immediate predecessor: SHA-256
  `4ce5a670fab4ae595fdef43871497c6b140b20ff0c08ae24de023adf18540aa2`.
  The default harness passed the same epoch-2 contract. CI/publishing continue
  to select the actual immediate predecessor, not a convenient pinned pass.
- RC18, a pre-reader binary: SHA-256
  `13c52796b5c5525fd09e45780c5cad0d3f8181d0602ddd16726b5e2d9cab94a4`.
  Its installed wheel refused the actual epoch-2 revision before any logical
  database or backup-artifact mutation. Refusal is **not-applicable**, not a
  passing rollback capability and never enables the build literal. Reproduce:
  `python scripts/sqlite_predecessor_contract.py --release-tag gh-v3.2.5rc19 --require-refusal`.

A supported rollback selects RC19 or newer without Alembic downgrade/restamping.
An unsupported older binary requires an explicitly approved verified pre-upgrade
restore with every writer stopped; **restore discards subsequent writes**. No
backfill/export, automatic restore, bulk deletion or VACUUM is provided.

## Storage benefit (2026-11-08)

Read-only aggregate sample: seven complete UTC days, 2026-11-01 through
2026-11-07. No message/tool payload was exported. All observed calls in this
sample were Vibey; it supplies no estimate for other backends (whose trace
writer is unchanged). To avoid crediting #3367 twice, the baseline sums UTF-8
bytes of `content_text`, `json_remove(content_json, '$.text')`, and
`metadata_json`, restricted to physical `tool_call`/`trace`/null-context rows.
SQLite's JSON normalization also compacts whitespace, so this is a conservative
logical-payload estimate, not byte-identical reserialization of every old row.
This excludes SQLite record/index overhead and normalizes away the old duplicate
JSON text copy rather than counting the reported pre-single-copy trace size.

| Cohort | Rows (7 days) | Payload bytes (7 days) | Mean bytes/day | Daily range |
| --- | ---: | ---: | ---: | ---: |
| Vibey traces, normalized single-copy | 79,367 | 97,363,208 | 13,909,030 | 6,978,936–20,181,797 |
| Canonical tool results, unchanged | 78,158 | 351,296,733 | 50,185,248 | 23,060,828–69,320,326 |

For an equivalent workload fully adopted by marked canonical responses, this
means **about 13.91 MB/day of redundant trace payload avoided** (observed range
6.98–20.18 MB/day), not bytes already removed. Existing/unmarked traces remain;
there is no backfill or bulk deletion. The last complete day alone gives
8,778 calls / 8,314,157 normalized trace bytes versus 45,916,843 untouched result
bytes. New reader display metadata and canonical results are common to both
stage-2 and reader-first baselines; the suppression change adds no per-call data.
No immediate database-file shrink or general hardware latency claim follows
from these logical payload measurements.

### Bounded paired writer probe

Three alternating pairs of 64 synthetic calls each, Python 3.12.12 / SQLite
3.50.4 on macOS arm64. Each case clones the same checkpointed epoch-2 fixture:
one marked response and one retained result per call, a 160-character synthetic
argument plus path/index, no production payloads. The real synchronous
`persist_agent_message` path runs with the binary proof set to 1 (single-copy
compatibility writer) or 2 (suppression); Activity streaming and Session ranking
remain enabled. WAL auto-checkpoint is disabled to measure newly written bytes.
This is a small local warm-cache probe, not a contended or cold-disk forecast.

| Per 64-call case (median of 3) | Single-copy writer | Suppression |
| --- | ---: | ---: |
| New physical tool-call rows | 64 | 0 |
| New raw trace payload bytes | 25,142 | 0 |
| New `agent_events` table pages / bytes | 9 / 36,864 | 0 / 0 |
| New event-index pages / bytes | 7 / 28,672 | 0 / 0 |
| WAL file growth (bytes) | 2,204,232 | 16,512 |
| Retained result payload bytes (unchanged) | 27,264 | 27,264 |
| Canonical response bytes (unchanged) | 44,790 | 44,790 |

All six cases preserved the same canonical-data digest, published 64 canonical
live step identities, and advanced Session activity ranking. Timing each complete
mirror call (transaction plus post-commit publish), 192 samples per mode, gave
p50/p95/p99 **1.077/1.770/3.914 ms** with traces and **0.783/1.511/3.725 ms**
with suppression. These samples establish the omitted INSERT/index/WAL work,
not a general latency guarantee. No trace rows from these new calls enter the
future retention-delete cohort; existing retention scans/policy are unchanged,
and retention runtime was not benchmarked. Neither case shrank its main DB file.

## Validation

Primary owner coverage: projection/service tests for tool-only responses,
parallel/reused ids, interval-bounded results, fork isolation, pending/cancelled
calls, hosted media/errors, legacy/marked mixed rows and read-only behavior.
Distinct integration risks: adapter commits/ToolStarted and mirror live gates,
trace compatibility/identity links, query ACL and query plans. Frontend tests
cover live upserts, explicit ordering and reload deduplication. Stage-2 coverage
adds fallback gates, real epoch-2 crash atomicity/snapshot preservation, real
released-wheel readers and all native/proxy hosted integration tests against
projection-only storage. No release, service restart, remote operation or bulk
cleanup is authorized here.
