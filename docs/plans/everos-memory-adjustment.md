# EverOS Memory Integration Adjustment (Discussion Baseline)

> Status: discussion baseline, not yet implemented
>
> Date: 2026-08-08
>
> Target branch: Avibe `dev`
>
> Related repository: EverOS at <https://github.com/avibe-bot/EverOS> (pinned revision listed in §2)

This document captures the research conclusions from the current Avibe ↔ EverOS Memory integration as a shared baseline for subsequent design, implementation, and review. It only defines direction, constraints, and migration order; it does not claim that any code is finished, nor does it describe capabilities EverOS does not currently expose as if they exist.

Constraint: this adjustment does not modify EverOS source code. Anything that requires EverOS to gain caller identity, receipt lookup, replay, rotation operation, or similar capabilities is recorded either as a future upstream capability or as a fail-closed Avibe-side downgrade, and is not a delivery prerequisite for this implementation phase.

Submission form: this plan targets the Avibe `dev` branch. Implementation lands together with code changes via the standard PR review flow into `dev`; it does not merge directly to `master`. This plan is the latest design baseline for the Memory adjustment cycle. `docs/plans/memory-processing-log-page.md`, `docs/plans/memory-architecture-deepening.md`, and `docs/plans/everos-1.2.1-upgrade.md` continue to serve as already-implemented history and constraint sources.

Sections superseded by this plan:

- `memory-processing-log-page.md`: treats the log page as the Memory status console, an independent status model, and the source of full provider-call payload records;
- `memory-architecture-deepening.md`: keeps an independent Memory status page, derives composite `ready/syncing/degraded` status, and mixes `status_payload.data_exists` with drain/embedding safety gates inside a user-visible status payload;
- `everos-1.2.1-upgrade.md`: per-delivery flush was required by the chat mode at the time; this plan removes that behavior and replaces it with idle/close/max-age triggers.

Sections retained from prior plans:

- `data_exists`, `/status`, CLI machine-readable fields, drain/embedding safety gates, the provider-root sentinel and owned-clear flow, `memory_capture_queue` durable enqueue and recovery, and the EverOS real-wheel contract test suite;
- the `everos-1.2.1-upgrade.md` derivation path where `project_id` is an HMAC digest of the Agent Session cwd and the permission isolation model;
- the existing processing-record page's precise memcell/capture/OME/provider-call linkage.

## 1. Conclusion Summary

### 1.1 Integration Form

Continue using **Avibe-managed EverOS child process + Unix Domain Socket (UDS) HTTP**. Do not switch to in-process Python imports of EverOS.

This HTTP is not a public-facing service and not a TCP service on the host — it is a private IPC mechanism:

```text
Avibe main process
    │ httpx + Unix Domain Socket
    ▼
Avibe-managed EverOS child process
    ├── Markdown
    ├── SQLite
    ├── LanceDB
    ├── Cascade
    └── OME
```

The current UDS sidecar, child-process lifecycle, credential isolation, and TCP-listener checks already form a sound foundation and should be retained. What needs adjustment is the upper interface and internal responsibilities, not the transport.

### 1.2 Flush Timing

Remove the "flush after every successful add" behavior.

Target policy:

1. Normal messages only call EverOS `/add` and let EverOS perform natural-boundary detection;
2. After a session idle timeout, perform a flush;
3. On `/new`, session archive, explicit session close, or explicit user request, perform a final flush;
4. Add a maximum unflushed duration to prevent long-running sessions from never reaching a boundary;
5. On Avibe shutdown, do not block-wait on a potentially minute-long LLM flush; persist the durable state and resume on the next start;
6. Final flush cannot rely on a single close hook. The contract is strict and ordered: acquire the session fence first, then check or wait for the target session's outbox drain, and hold the fence through the flush while persisting this generation's watermark. Waiting for drain without first holding the fence is forbidden — a worker that observes a drained outbox can still start a new provider add and race into the closing generation, and messages added after close will then miss their subsequent flush trigger.

The initial idle window is suggested as 5 minutes; the precise value must be confirmed against actual LLM cost and interaction feel.

### 1.3 Processing-Record Page and the Minimum Internal Reliability Loop

Do not maintain a separate status page that tries to express a complete Memory state. Fold the status summary into the existing processing-record page; the page only shows what can be reliably read right now:

- the most recent EverOS `/health` summary;
- EverOS version and capabilities;
- Cascade health, pending count, retryable/permanent failure counts (if `/health` exposes them);
- recorder state;
- the existing capture → add/flush → memcell → OME → indexing pipeline;
- confirmed anomalies, recovery records, and data-unavailable notices.

Do not modify EverOS and do not add a new EverOS business-metrics contract. The current `/metrics` only carries HTTP request counters and durations and is not a core data source for the Memory page. The page must label every fact with its observation time and distinguish current summary, historical record, expired data, and unavailable data.

The outbox and sidecar lifecycle are retained but kept at the minimum loop required for reliability:

- outbox: local persistence, claim, call `/add`, bounded retries, success settlement that clears the payload, boot recovery;
- lifecycle: start/stop the managed EverOS child, UDS availability, crash recovery, call fencing during clear/restart;
- do not maintain a complex metrics tree for the page;
- do not derive a composite `ready/syncing/degraded` from multiple sources;
- do not add processing-endpoint probes or deep provider-root scans to ordinary log-page reads; the existing `/status` route keeps the drain/embedding safety-gate and contract checks for now, and is revisited once call sites migrate.

In short, simplifying the page does not delete the reliability mechanisms — reliability state stays internal to delivery and recovery; the user surface only shows what can be directly confirmed.

### 1.4 Data Protection and Rebuild

EverOS Markdown is the source of truth for already-extracted business memory; LanceDB is a derived index that can be rebuilt. The full safety boundary is not "Markdown + unprocessed buffer" alone; it must include:

```text
Avibe capture outbox
+ EverOS unprocessed_buffer
+ EverOS memcell
+ Markdown
```

Until `/add` is acknowledged, the Avibe outbox is the recovery source. After a message reaches EverOS, content not yet bounded stays in `unprocessed_buffer`; raw dialog units after boundary live in `memcell`; extracted business memory lands in Markdown.

Forbidden:

- deleting the entire EverOS `.index`;
- deleting `.index/lancedb` and expecting automatic recovery;
- recursively deleting a provider root from inside Avibe.

Index recovery must call EverOS's controlled rebuild operation and respect the maintenance lock and queue-reset order.

### 1.5 Embedding Key and Model Change

- **API key only**: semantics do not change; no rebuild required — restart the EverOS child process and reuse the original provider root.
- **Model, effective dimension, normalization, truncation, preprocessing, or actual model semantics change**: treat as a new embedding space; full rebuild of every embedding-dependent derived state is required.

A full rebuild is not LanceDB alone. Cluster centroids live in EverOS SQLite and are also vector-derived state, so they must be recomputed or explicitly invalidated. Profile, agent skill, and reflection artifacts derived from clusters/embedding must also be folded into the rebuild plan.

Until a complete rotation operation is in place, retain the current fail-closed behavior and refuse to silently mix vector spaces over existing data.

### 1.6 The Four Search Modes

Avibe should consume EverOS's four search modes through its own policy surface:

- `keyword`: precise terms, IDs, error codes, command names;
- `vector`: semantic similarity and paraphrases;
- `hybrid`: the default general-purpose search;
- `agentic`: complex multi-step reasoning searches; enabled explicitly and constrained by capability, cost, and timeout.

Do not leak EverOS DTOs upward. Avibe exposes only provider-neutral policy; the adapter maps to EverOS.

The Profile target adapter uses the owner-keyed EverOS `/get(memory_type="profile")` path because profile scope is user-global (§14); the search-literal `"profile"` workaround stays as a fallback for hosts that prefer it. EverOS `/get` reads a single profile row keyed by owner with no reliable project filter, but that limitation is moot under the user-global scope (see §9.3).

The current session's unprocessed messages can serve as short-term context, but only bound to Avibe's trusted canonical current session. Arbitrary `filters.session_id` from the caller must never be forwarded as an overlay query. The overlay must make its source explicit — EverOS `unprocessed_buffer` — and must not represent Avibe outbox content or Markdown written but not yet Cascade-projected.

## 2. Research Scope and Version Baseline

This plan is based on the following source state:

- Avibe `dev`: `fbd406eab933fc84e88b10a4a6087c793ab6fc11`, verified against `origin/dev`;
- EverOS: `https://github.com/avibe-bot/EverOS`, pinned revision `48fc908` (current `origin/main`) for the canonical reference. The author's local working tree additionally held `560fb80` plus user-prepared knowledge-base files; the local-only files are not part of the plan's evidence and contributors should rely on the remote-pinned revision plus the Avibe-side source references for verification.
- EverOS `v1.2.3` release notes are the only delta between the author's local tree and `origin/main` at the time of writing and do not change the code surface this plan depends on.

The current Avibe runtime still pins EverOS 1.2.1:

- `core/memory/artifact.py:35-42`
- `scripts/memory_runtime/pyproject.toml`

The EverOS source metadata is already 1.2.3 and requires Python 3.12:

- `EverOS/pyproject.toml:1-8`

Therefore the 1.2.3 upgrade is independent runtime artifact, compatibility, and release work; it cannot be completed by pointing Avibe at the author's local working tree.

## 3. Current Implementation Facts

### 3.1 Avibe Current Call Path

Current Memory is mostly built from:

| Module | Current responsibility |
|---|---|
| `core/memory/runtime.py` | Controller-owned lifecycle, artifact activation, sidecar supervision, worker start/stop, config reconcile |
| `core/memory/module.py` | Provider-independent capture/search/profile/status/clear |
| `core/memory/everos.py` | EverOS HTTP requests, response mapping, provider error classification |
| `core/memory/sidecar.py` | In-child route/shape/attachment validation and EverOS app start wrapper |
| `core/memory/process.py` | Python runtime, child process, UDS, owner/reaping, startup health check |
| `core/memory/store.py` | Avibe-local capture queue, delivery, flush observation, recovery |
| `core/memory/worker.py` | Queue claim, EverOS add, per-message flush, breaker, boot recovery |
| `core/memory/everos_insight/` | Provider-call recorder, processing log, version-coupled read adapter |

The most valuable existing seam is `MemoryProviderPort`, but today the worker still carries too much EverOS lifecycle semantics. The follow-up should split session-flush and status-collection into internal modules without enlarging the upper interface.

### 3.2 Current Flush Defect

`MemoryWorker.drain()` calls `_flush_session()` after every successful delivery:

- `core/memory/worker.py:168-182`
- `core/memory/worker.py:287-315`

This breaks EverOS's design of recognizing natural boundaries across continuous messages, increases LLM invocations, and fragments a continuous conversation into overly thin memory cells.

### 3.3 Current Search Limits

Avibe's current `EverOSPort` sends only:

```json
{
  "method": "hybrid",
  "include_profile": true,
  "enable_llm_rerank": false
}
```

Relevant locations:

- `core/memory/everos.py:386-413`
- `core/memory/sidecar.py:296-311`

The sidecar guard also restricts:

- user-owner only;
- `hybrid` only;
- cannot pass filters/radius/min_score;
- profile fetched via search query `"profile"`.

### 3.4 Current Status Model

Avibe's current status is assembled from:

- local queue stats;
- provider `/health`;
- processing-endpoint probes;
- disk space;
- runtime error;
- provider root/data existence;
- recorder health;
- flush observations.

Main locations:

- `core/memory/module.py:348-420`
- `core/memory/module.py:592-635`
- `core/memory/runtime.py:542-580`
- `core/memory/runtime.py:1407-1429`

Not all of this should be deleted, but it should not be re-executed on every status request.

### 3.5 Current EverOS Metrics and Health

EverOS `/metrics` is currently produced only by the Prometheus HTTP middleware:

- `EverOS/src/everos/core/middleware/prometheus.py:25-41`
- `EverOS/src/everos/entrypoints/api/routes/metrics.py:14-20`

It carries only:

- HTTP request counter;
- HTTP request duration histogram.

EverOS `/health` exposes capabilities and cascade health, but its `status="ok"` is explicitly liveness, not full business readiness:

- `EverOS/src/everos/entrypoints/api/routes/health.py:78-90`
- `EverOS/src/everos/entrypoints/api/routes/health.py:109-150`

Therefore this page simplification does not depend on `/metrics` and does not wait for EverOS to add business metrics. Avibe may remove the page-facing complex status collection and composite derivation, but must keep the minimum internal state needed for outbox delivery and sidecar lifecycle.

## 4. Target Architecture: One Deep Module, Multiple Internal Implementations

### 4.1 Avibe Public Interface

Recommended consolidation to three operations:

```text
capture(CaptureRequest) -> CaptureReceipt
recall(RecallRequest) -> RecallResult
operate(MemoryOperation) -> OperationReceipt
```

`operate` initially covers owner/admin-gated `restart` and `clear` only; projection rebuild, embedding rotation, and other operations that need stronger journal/fence/reconciliation stay as internal operations and are not exposed as ordinary public API.

The status page no longer relies on a new, semantically heavy `snapshot()` interface. The processing-record page uses the existing log reader and lightly reads EverOS `/health` at the top.

#### `capture`

- The return value means Avibe has written the message to the local durable outbox;
- Does not wait on EverOS, LLM, embedding, extraction, or Cascade;
- Idempotent on a stable source-message identity;
- Does not promise immediate searchability after return.

#### `recall`

- Uses the Avibe-owned `RecallRequest`;
- Allows specifying the search policy and freshness policy;
- Returns an Avibe-owned result, not an EverOS DTO;
- May carry freshness metadata such as `unprocessed` and `eventual`.

#### `operate`

- Unifies maintenance operations: `restart`, `clear`, projection rebuild, embedding rotation;
- Long operations return an operation identity;
- Lifecycle exclusivity and data safety are owned inside Memory;
- No new global status machine per operation; the page only shows confirmed operation results and processing records.

### 4.2 Internal Modules

```text
Avibe Memory interface
    │
    ├── Durable capture store
    ├── SessionFlushCoordinator
    ├── ProcessingRecordView
    ├── MaintenanceCoordinator
    │
    ▼
MemoryEngine seam
    │
    ▼
EverOS UDS HTTP adapter
    │
    ▼
Pinned EverOS child runtime
```

Upper layers must not know about `/add`, `/flush`, `include_profile`, LanceDB, Cascade, OME, or EverOS error envelopes. Those live in the adapter or internal coordinators.

## 5. Flush Target Design

### 5.1 Target Sequence

```text
message
  │
  ▼
Avibe durable outbox
  │
  ▼
EverOS /add
  │
  ├── status=accumulated: stays in EverOS unprocessed_buffer
  └── status=extracted: EverOS has completed a boundary extraction
  │
  ▼
natural session boundary / idle / explicit close
  │
  ▼
EverOS /flush
  │
  ▼
Markdown is on disk
  │
  ▼
Cascade asynchronously projects to LanceDB
```

`/add` success does not mean searchable; `/flush` success does not mean LanceDB has projected. The UI and callers must preserve eventual-consistency semantics.

### 5.2 Flush Triggers

| Trigger | Fires? | Note |
|---|---:|---|
| Every successful add | No | Avoid fragmenting continuous dialog |
| EverOS `/add` natural boundary | Decided by EverOS | Avibe does not append pointless flushes |
| Session idle timeout | Yes | Initial proposal: 5 minutes |
| `/new` or session close | Yes | final flush |
| Session archive | Yes | Hook into the unified lifecycle |
| User explicitly requests immediate memory landing | Yes | Internal interface keeps bounded-wait semantics; first-version UI does not expose it; revisit after Phase 3 product decisions |
| Maximum unflushed age / message count | Yes | Prevent unbounded accumulation |
| Avibe shutdown | No synchronous wait | Persist `due` state and resume on next start |

### 5.3 Flush Generation Invariants

Each `(principal_id, epoch, project_ref, session_id)` owns an independent flush generation. The `app` value is retained as origin metadata, but it is not an alternate flush identity. The canonical key is the **provider session reference** derived as `_provider_session_ref(scope_key, principal_id, project_ref, session_id, meta.epoch)` (`core/memory/store.py:302-308`); the same reference is what the worker passes to both `/add` and `/flush` (`core/memory/worker.py:240-250, 287-298`), so the identity Avibe fences and flushes is the same identity EverOS sees on the wire. A logical session identifier alone is not sufficient: two principals reusing one logical session ID, or one session surviving an epoch change (e.g. after a manual restart), must not collide on the same fence. The canonical key therefore preserves principal_id and epoch:

- `principal_id`: derived `u-…` value, derived from `runtime.principal_for_user_key()` for UI callers and asserted for CLI / API callers (see §11.4);
- `epoch`: the current `memory_meta.epoch` value at enqueue time, taken from `meta.epoch` in `MemoryStore.enqueue_request`;
- `project_ref`: scope identifier matching the project filter that the read adapter enforces (§11.4);
- `session_id`: the logical session identifier supplied by the caller, kept as the human-facing label;
- `app`: the backend or surface identifier that originated the capture (UI, CLI, agent, IM), retained for provenance but excluded from `provider_session_ref`.

The canonical key must be uniform across capture, add, flush, log, and recovery paths. Mixing `(session, project)`, `(app, project, session)`, or any principal/epoch-augmented variants across modules is forbidden — fence, watermark, recovery, and EverOS wire identity must all reference the same derived `provider_session_ref`. Whenever the enqueue path reads `principal_id`, `project_ref`, `session_id`, or `meta.epoch`, the resulting tuple is recorded as the canonical session reference; coordinator code (fence acquisition, flush marker, recovery scan, processing-record projection) keys off that reference, never off the bare logical `session_id` alone.

- Before flush begins, **acquire the session fence first**, then either stop new provider adds for the target session or wait for its outbox drain; the fence is retained through the flush so any concurrent worker that observes a drained outbox cannot start a new provider add without entering the next generation;
- Persist generation, the watermark of confirmed adds, and the fence epoch;
- During an in-flight flush, new messages can only enter the next generation;
- Settlement may only update the fenced generation, not all historical delivery rows of the session/project;
- At most one flush per session at a time;
- `unknown` outcomes cannot auto-replay without bound;
- A new worker first recovers `in_flight` then processes `due` / `not_attempted` with batched backoff to avoid restart storms;
- Different sessions may run concurrently but are bounded by the global provider concurrency cap.

### 5.4 Upstream Recovery Gap

EverOS's boundary logic writes memcell first, then replaces the unprocessed buffer, then runs the downstream pipeline:

- `EverOS/src/everos/service/_boundary.py:154-198`
- `EverOS/src/everos/service/memorize.py:236-279`

If the process crashes between these steps, memcell may exist, buffer state may have shifted, and Markdown / OME pipelines may not have completed. This means exactly-once extraction cannot be claimed under those conditions.

Without caller message identity, flush operation/generation identity, or receipt lookup on the EverOS side, Avibe cannot treat an unknown outcome as safely replayable. Target recovery contract:

- `unknown` means the provider may have committed;
- Avibe persists `unknown`, the generation, and the fence; it does not auto-replay;
- Auto-progression is only allowed with proven `not_committed` or a valid receipt/reconciliation;
- When confirmation is impossible, hold at `manual_required` rather than permanently masking success or running unbounded at-least-once replay;
- Extraction receipt/ledger, caller stable identity, and memcell-to-Markdown replay belong to a future EverOS capability and are not a prerequisite for the "no EverOS modification" implementation phase.

### 5.5 Manual Resolution Path for `manual_required`

`manual_required` is a fence, not a permanent stuck state. When an add or flush lands here, the operator must have an audited resolution path that lets the watermark and fence advance without resorting to `restart` (which cannot tell whether the write committed) or `clear` (which is destructively broader). The shape:

```text
manual_required detected
  → durable record: session, generation, fence_epoch, operation_id, last_known_state,
                    operation_kind (add | flush), last_observed_outcome
  → operator runs an audited manual resolution through `operate` (Phase 4).
    Branches are split per operation_kind so an ambiguous /flush is settled,
    not just an ambiguous /add:

    For operation_kind = add:
      case A — confirmed committed via out-of-band evidence:
          advance watermark to the highest observed acked add;
          release fence on the same generation; resume normal flow.
      case B — confirmed not_committed:
          roll the unacked add back to the outbox;
          retain the session fence through the retry; do not release the
              fence until the rolled-back add is acknowledged (case A) or
              an operator records a terminal decision (case C); releasing
              the fence here would let a waiting worker submit a newer
              add to the same EverOS session first, and EverOS has no
              generation identifier, so same-session serialization alone
              does not preserve the original add's ordering; the later
              retry could then land in the wrong boundary and invalidate
              generation watermarks or flush contents;
      case C — inconclusive but stale (> 24h or after explicit operator decision):
          record operator decision in the journal; advance watermark conservatively;
          release fence; do not silently auto-replay.

    For operation_kind = flush:
      case D — confirmed flush committed (Markdown on disk, cascade-pending OK):
          mark flush_state=settled on the same generation;
          advance watermark to the post-flush boundary;
          release fence; resume normal flow.
      case E — confirmed flush not committed:
          restore the generation's flush_state to due (or pre-due if boundary
              must be re-detected);
          leave add rows in place;
          retain the session fence through the retry; do not release the
              fence until this generation's flush completes (case D) or an
              operator enters case F; releasing the fence here would let a
              worker send newer-generation messages into the same EverOS
              session before the retry, and the retried `/flush` could then
              consume both generations and invalidate their watermarks;
          allow retry only against the same generation's flushed payload.
      case F — inconclusive but stale:
          record operator decision; mark flush_state=settled_with_caveat;
          advance watermark conservatively; release fence;
          do not silently auto-replay.

  → resolution writes an audit row: actor, decision, evidence pointer,
    generation advanced, watermark advanced, flush_state advanced
  → page surfaces the audit row in the processing-record page so manual_required is observable
```

Constraint: `restart` is not a valid resolution for `manual_required` because it cannot prove the write's outcome. `clear` is not a valid resolution either because it is broader than the unknown write. The audited `operate` path above is the only sanctioned transition. Until the journal/fence is implemented in Avibe, `manual_required` keeps the fence in place and forbids auto-replay, but does not block unrelated sessions.

### 5.6 Fingerprint Resolution Operation

The fingerprint-specific recovery operation is a third audited `operate` action distinct from `add` and `flush`. Its `operation_kind` is `fingerprint_resolve`, and it is the only sanctioned transition for an unknown-fingerprint legacy root (§8.1.1 case 2). The shape:

```text
fingerprint_resolve detected (case-2 unknown-fingerprint legacy root)
  → durable record: provider_root_id, fence_epoch, operation_id,
                    operation_kind=fingerprint_resolve,
                    last_known_state, evidence_pointer
  → operator declares one of the following audited intents:
      case R1 — full semantic rotation (proceed to §8.3 / §8.4):
          take restorable snapshot, rebuild LanceDB vectors / FTS / scalar
              indexes, recompute cluster centroids, invalidate embedding-
              dependent OME state, reprocess profile/skill/reflection
              derived artifacts, write new fingerprint; on failure restore
              from snapshot.
      case R2 — accept the legacy vector space as authoritative and
                pin its fingerprint:
          compute fingerprint from a verifiable legacy source (provider-
              root sentinel artifact metadata, companion store digest, or
              Avibe build-stamp catalogue — see §8.1.1 case 1); persist
              the fingerprint with an audit row recording the legacy
              evidence pointer and operator decision; on any later
              configuration change the case-2 verdict re-arms.
      case R3 — refuse and abort recovery (record only):
          write the unknown-fingerprint verdict to memory_meta if not
              already there; release no fence; require an explicit
              operator decision before retry.
  → resolution writes an audit row: actor, decision, evidence pointer,
    fingerprint before/after, fence_epoch, journal phase
  → page surfaces the audit row in the processing-record page so unknown
    fingerprint is observable
```

The fingerprint resolution operation is closed to chat/agent/API call paths and reachable only through the same audited `operate` interface that powers Phase 4, with its own per-root maintenance lock. §5.5 cases A–F are unchanged because they remain scoped to add/flush; the case-2 legacy root cannot reach them and must go through `fingerprint_resolve`.

## 6. Processing-Record Page Design

### 6.1 Page Form

Delete the standalone Memory status page; fold the status summary into the processing-record page:

```text
Memory
├── Processing record
│   ├── EverOS runtime summary
│   ├── Recent pipeline
│   ├── Anomalies and recovery
│   └── Diagnostic detail
├── Profile
├── Search
└── Settings
```

The top of the page shows only the most recent successfully-read EverOS `/health` summary:

- EverOS version;
- capabilities: LLM, embedding, reranker, parser, agentic;
- Cascade `healthy`, `pending`, retryable/permanent failure counts;
- Cascade reasons;
- Avibe recorder state.

Every summary must carry `observed_at` or an equivalent observation time. Reads that fail, are missing, or are stale must render as `unknown/unavailable`; they cannot be promoted into `ready` or "all memory is done."

### 6.2 Pipeline

Continue using the existing processing log; do not add a new EverOS business-metrics protocol or a private SQLite status interface:

```text
capture
  → add / flush
  → memcell
  → episode
  → OME strategy
  → profile / skill
  → indexing
```

Only show records that the existing provenance can confirm. For missing database, lock, format error, or stale link cases, render the affected step as unavailable and keep the rest.

### 6.3 Deleting the Composite Status Derivation

The following are no longer part of the user-visible status model and must not be recomputed on ordinary log-page reads:

- `ready/syncing/degraded` composite state tree;
- full pending/succeeded/dead/missed metric tree for the Avibe outbox;
- provider-root existence checks and deep scans;
- processing-endpoint active probes;
- cross-interface precedence;
- using one successful `/health` to claim Memory pipeline ready.

The `/status` route stays as an internal contract (CLI, UI settings, save validation, drain/embedding safety gate all depend on it), and its probes plus `data_exists` derivation are out of scope for this simplification; the new processing-record read path does not trigger them.

The page may show directly confirmed single facts — the most recent `/health` failure, a specific add/flush failure, a specific recorder degraded — but it must not combine them into a total state beyond what the source semantics justify.

### 6.4 `/metrics` Handling

EverOS `/metrics` today only carries HTTP request counter and duration histogram:

- `EverOS/src/everos/core/middleware/prometheus.py:25-41`
- `EverOS/src/everos/entrypoints/api/routes/metrics.py:14-20`

Under the "no EverOS modification" constraint, do not make `/metrics` a core Memory-page dependency and do not maintain an Avibe-side private EverOS business-metrics protocol. If EverOS later offers a stable business-metrics surface, it can become an optional data source — but it is not a prerequisite for this adjustment.

### 6.5 Minimum Responsibility for Outbox and Lifecycle

#### Outbox minimum loop

The outbox only:

1. persists the capture payload;
2. deduplicates by a stable identity;
3. claims a pending delivery row;
4. calls EverOS `/add`;
5. applies bounded retries to confirmed-retryable errors;
6. only allows success settlement and sensitive payload cleanup on a structurally complete, status-supported provider ack; malformed 2xx is treated as `unknown`/`manual_required` with the recoverable data preserved;
7. resumes incomplete claims after Avibe restart.

To support idle/max-age flush, Avibe-owned durable state must at minimum add: `generation`, `first_unflushed_at`, `last_add_ack_at`, `due_at`, `next_attempt_at`, `flush_state`, `watermark`, and `fence_epoch`. The idle timer is owned by a long-running Avibe controller/runtime task; restart recovers `due` sessions from the database and processes them in batches under the global concurrency cap and backoff window to avoid restart storms. Continuous new messages update idle `due` only — they must not reset `first_unflushed_at`, otherwise max-age starves. When `/add` returns `status=extracted`, that acknowledgement advances the watermark for the current generation and recomputes the next generation's `first_unflushed_at` from any remaining unacked messages; this prevents a long-running session where natural-boundary extractions dominate from inheriting the age of already-extracted messages and repeatedly hitting max-age.

The outbox does NOT:

- compute global Memory ready status;
- interpret EverOS Cascade/OME internal state;
- supply a full business-metrics dashboard for the UI;
- replace EverOS's Markdown, memcell, or unprocessed buffer.

#### Lifecycle minimum loop

Lifecycle only:

1. prepares and starts the managed EverOS child;
2. confirms UDS availability;
3. reaps and restarts on exit or loss;
4. stops the old child on restart/clear/config swap to prevent shared-root concurrent use;
5. on shutdown, stops the worker, recorder, and child.

Lifecycle does NOT:

- derive a complex Memory state through layered probes;
- privately scan EverOS internal queues;
- maintain a second provider-state database;
- guarantee a real-time "all stages converged" promise to the page.

These two modules remain internal reliability mechanisms; their state is not extended into a product-level status model that users must understand.

## 7. Data Persistence, Backup, and Rebuild

### 7.1 Data Layers

| Layer | Role | Reconstructible from Markdown? | Protection requirement |
|---|---|---:|---|
| Avibe capture outbox | Local durable intake before `/add` | No | Must protect until provider receipt or explicit recovery |
| EverOS `unprocessed_buffer` | Received but not yet bounded raw messages | No | Must protect |
| EverOS `memcell` | Raw dialog units after boundary | Today, do not assume Markdown-rebuildable | Must protect until extraction recovery contract is in place |
| Markdown | Source of truth for already-extracted business memory | Itself | Must protect |
| `md_change_state` | Cascade queue and LSN | Reconstructible, but controlled reset is safer | Reset in order during rebuild |
| OME SQLite | Async strategy run state | Partly reconstructible | Decide replay during maintenance |
| LanceDB | Vector / BM25 / scalar derived index | Rebuildable from Markdown | Never `rm`; use EverOS rebuild |
| Cluster centroid | Embedding-derived SQLite vectors | Not a normal index | Must be recomputed or invalidated on rotation |

### 7.2 Backup Scope

The safest default backup is a consistent snapshot of the entire provider root plus Avibe-owned state and the attachments directory. The paths must be resolved relative to the effective Avibe home (`config.paths.get_vibe_remote_dir()`), not hardcoded as `~/.avibe/...`. Concretely:

```text
{avibe_home}/state/memory/memory.sqlite
{avibe_home}/state/memory/rotation_replay.sqlite
{avibe_home}/memory/everos-root/
{avibe_home}/memory/call-log/call-log.db
{avibe_home}/attachments/avibe/
```

If `AVIBE_HOME` points outside the default home, or a legacy home remains active because migration could not complete, hardcoded literal paths can produce an apparently successful backup that omits the live capture database, provider root, call log, or attachments. Always resolve via `get_vibe_remote_dir()` (or the resolved runtime home) at backup time, including after a legacy rename.

The capture queue today only stores attachment URI/metadata (`core/memory/attachments.py:12-59`) and does not copy attachment bytes; if the backup strategy does not include attachment originals, accepted captures cannot be replayed. Before implementation the attachment policy must be explicit: Workbench uploads already live in the Avibe-owned private attachment store; the default recommendation is to pin a durable attachment reference into the pending capture row at acceptance time and to include that directory in the consistency snapshot, with explicit size/retention and recovery validation. If the product does not provide attachment copy or backup guarantees, capture can only declare that metadata is saved and cannot promise attachment recoverability.

Must cover at minimum:

- Markdown;
- `system.db` unprocessed buffer and memcell;
- Avibe capture queue;
- attachment bytes (if capture is allowed as a recoverable input);
- OME state (if exact recovery of async tasks is required);
- call log (if processing diagnostics must be preserved).

Never copy a live SQLite file mid-write; pause the relevant process or use SQLite backup/snapshot semantics, and let the attachment snapshot and the queue snapshot share an explicit consistency fence.

### 7.3 Projection Rebuild

Applies to LanceDB corruption, schema drift, or index drift. Today EverOS exposes this only as a CLI maintenance operation; it is not a regular business HTTP operation that Avibe can call. Avibe must not describe "calling controlled rebuild" as if it were an existing API capability.

1. Create and persist an `operation_id`, operation phase, provider-root fingerprint, fence epoch, and owner;
2. Acquire the exclusive maintenance lease;
3. Fence Avibe new delivery and old-epoch provider calls;
4. Stop the EverOS server;
5. Reset the Cascade queue first;
6. Delete and rebuild only the LanceDB business tables;
7. Scan all Markdown;
8. Wait for and verify Cascade drain;
9. Update the operation journal with each phase's result;
10. Start the sidecar and resume delivery;
11. On startup, follow the journal to resume, roll back, or fail closed; mid-flight crash must not be treated as success.

All destructive phases must have fault-injection tests. Late writes from the old epoch must be rejected or discarded and never written into the new generation/root.

EverOS's current controlled implementation already fixes the order: "reset queue first, then drop tables":

- `EverOS/src/everos/entrypoints/cli/commands/cascade.py:494-529`

Avibe must not copy the underlying logic itself; it must call the controlled EverOS operation.

### 7.4 Disallowed Recovery Actions

```text
rm -rf .index
rm -rf .index/lancedb
```

Reason:

- Deleting the entire `.index` deletes the not-yet-extracted `unprocessed_buffer`;
- Deleting only LanceDB leaves a done queue so the scanner considers files already processed and the index can be restored empty.

EverOS `docs/storage_layout.md` and `docs/how-memory-works.md` still contain the older "entire `.index` is deletable" wording and should be unified upstream to the stricter recovery contract.

## 8. Embedding Rotation

### 8.1 Semantic Fingerprint

Suggested definition:

```text
embedding_semantic_fingerprint = hash(
    provider semantic identity,
    effective model identity/revision,
    output dimension,
    dimensions parameter,
    normalization,
    truncation,
    preprocessing/tokenizer revision,
    vector/index schema revision,
)
```

The API key is not part of the fingerprint.

The fingerprint must be persisted in Avibe-owned Memory metadata and compared against the candidate configuration at every startup and reconcile; it cannot live only in-process. Unknown model identity, missing fingerprint, or unprovable semantic equivalence must fail closed. The provider-root sentinel may record the artifact fingerprint that created the runtime, but it does not replace the semantic embedding fingerprint.

Include only factors that actually change the vector space; for example, an equivalent proxy base-URL change must not force a rebuild on the URL string alone, but unprovable equivalence must fail closed.

#### 8.1.1 Fingerprint Bootstrap for Existing Roots

Enforcement of the semantic fingerprint lands behind a one-time migration so existing installations do not fail closed at startup. The bootstrap must be honest about what the legacy persistence actually records — the current Avibe `MemoryEndpointConfig` only persists `base_url`, `model`, and `api_key` (`config/v2_config.py:280-298`), and `memory_meta` carries no historical model revision, output dimension, normalization, truncation, or vector/index schema fields (`core/memory/schema.sql:2-30`). A legacy root therefore has no verifiable record of the vector space that built its existing data; model aliases or changed runtime defaults can silently mix incompatible embeddings. Three cases:

1. **Legacy root with a verifiable legacy fingerprint source.** A legacy root only enters this case when one of the following is true: (a) the provider-root sentinel artifact metadata already records a recorded embedding model identity/revision, output dimension, and vector/index schema revision; (b) a known companion store (LanceDB business table fingerprint, cluster centroid digest, or `embedding_semantic_fingerprint` row written by an earlier build that pre-dates this rule) still names the model identity/revision, output dimension, and vector/index schema revision that produced the data; or (c) an Avibe-side build stamp can be tied to a published fingerprint catalogue covering the recorded embedding endpoint. In these cases only, seed the fingerprint from the verifiable legacy source (`provider`, model identity/revision, output dimension, normalization, truncation, vector/index schema revision at the time the root was created). The seed is durable in `memory_meta` and treated as equivalent to a freshly computed fingerprint until the next configuration change.
2. **Legacy root with no verifiable legacy fingerprint source, a recorded configuration-change marker, or a persisted embedding configuration that no longer matches the candidate.** Treat as an unknown fingerprint; refuse to start. The operator must run a fingerprint-specific recovery operation (§5.6) before retry. Do not silently reseed — that would silently accept a vector-space mismatch and poison later searches with mixed embeddings.
3. **Newly created or freshly empty root.** Compute the fingerprint at activation time against the candidate and persist before the first `/add`.

The migration is idempotent and runs once at startup when `memory_meta` lacks the fingerprint row. After bootstrap, the fail-closed rule in §8.1 applies normally; cases 1 and 3 transition into the same steady-state as the rest of §8. The case-2 verdict is itself recorded in `memory_meta` so a later boot reads the same unknown-fingerprint state without re-deciding.

### 8.2 Key-Only Rotation

```text
acquire per-root maintenance lock
→ fence provider calls
→ validate new credential
→ stop old sidecar
→ start new sidecar with new key
→ reuse original provider root
→ verify capability
→ restore claims
→ on success: release maintenance lock
→ on failure to start new sidecar OR on capability verification failure
    AFTER the old sidecar has stopped:
    - restart the old sidecar against the original provider root
      using the original credential
    - verify capability under the original credential
    - only then release the maintenance lock; the provider-call fence
      is held continuously through the failed attempt and the
      restoration
    - if the original sidecar cannot be restarted against the
      original root, hold the lock, journal `key_rotation_restore_failed`,
      and surface the failure on the processing-record page; the
      operator chooses between another restore attempt and a fenced
      `clear` (see §15 acceptance)
```

Does not delete Markdown, SQLite, or LanceDB; does not trigger a full rebuild. The lock-and-fence pairing is what prevents the failure mode where the old sidecar has stopped, the new sidecar cannot start, and Avibe has no recorded plan for putting the old sidecar back: without an explicit restore arm in the sequence, the runtime can be left with no live sidecar and no released fence. The same rule applies whether the failure is a credential validation failure, a new-sidecar start failure, or a capability verification failure after the old sidecar is gone; in every case, releasing the lock requires a verified live sidecar on one of the two known credentials.

### 8.3 Full Semantic Rotation

The first version is offline-stoppage with a restorable snapshot. The sequence below always takes a verified snapshot of the current state before mutating any derived data, so a failed rotation can roll back to that snapshot instead of leaving the old generation "half-mutated". The first version does not promise atomic cutover or blue-green rebuild; those are evaluated later.

```text
acquire maintenance lock
→ fence capture delivery / provider calls (Avibe outbox stops accepting
    new capture intake; provider calls from the active generation stop)
→ quiesce writers under the lock:
    - stop EverOS (no /add, /flush, /search, /status, /clear against the
      active provider root — the active root is offline)
    - pause capture acceptance at the Avibe sidecar intake for the entire
      rotation window (capture requests are rejected with an explicit
      `maintenance_in_progress` reason, not queued); the durable capture
      queue must not receive rows during this window, and the live root
      must not be visible to any caller
    - drain the Avibe outbox: every pre-quiescence capture row has either
      been committed to the legacy root (and is therefore inside the
      snapshot window) or is durably moved to the rotation replay store
      with `pending_rotation_replay` before it is removed from the
      snapshot-covered outbox; no row may remain in the snapshot-covered
      outbox unless it has already been committed to the legacy root, and
      no row may be added to the snapshot-covered outbox after the
      snapshot step begins
→ take a restorable snapshot of: Markdown tree, system.db (unprocessed_buffer
    + memcell + md_change_state), ome.db, LanceDB business tables, cluster
    centroids, the current semantic embedding fingerprint, and Avibe outbox
    (the already-moved `pending_rotation_replay` rows are outside this
    snapshot and remain quarantined until the new sidecar is live)
→ verify snapshot integrity (checksum, file presence, sqlite backup API)
→ save and verify current root
→ preserve Markdown, unprocessed_buffer, memcell, Avibe outbox snapshot
→ rebuild LanceDB vectors and FTS/scalar indexes
→ recompute cluster centroid and cluster membership
→ rebuild or invalidate embedding-dependent OME state
→ reprocess profile/skill/reflection derived artifacts
→ write new fingerprint
→ start new sidecar
→ replay quarantined `pending_rotation_replay` rows against the new root
    in their original order; rows that fail to deliver stay in the rotation
    replay store as `pending_rotation_replay`
→ replay acknowledgements do not finalize the row into the existing
    success-settlement path (`core/memory/store.py:491-500`); the row is
    marked `delivered_pending_rotation_commit` with the new sidecar's
    `add_request_id`, the `payload_text` / `payload_attachments` columns
    stay populated, and the row remains in the rotation replay store
→ verify projection convergence
→ on any post-snapshot failure: stop, restore from the snapshot, mark the
    operation journal as failed at the failing phase, never claim the old
    generation is intact unless the snapshot restore actually completed;
    the rotation replay store is left untouched on disk and replay is
    retried on the next rotation or operator replay; `delivered_pending_rotation_commit`
    rows roll back to `pending_rotation_replay` because their durable copy
    survived in the separate store outside the snapshot scope
→ on success: only after projection convergence verifies does the rotation
    journal commit, and only at that point do `delivered_pending_rotation_commit`
    rows transition through the normal success settlement path and are
    removed from the rotation replay store; the snapshot drops only after
    both projection convergence verifies and the rotation replay store
    has been fully drained or returned to a recoverable state
→ on rotation-success terminal: journal commit completed and snapshot
    dropped ⇒ release the maintenance lock; if the rotation was
    successful (new sidecar live, fingerprint written) the new sidecar
    remains the only owner of the provider root; if the rotation was a
    rollback-to-original (a separate "cancel-and-restore" terminal after
    a partial rebuild was abandoned), restart the restored old sidecar
    against the restored provider root, verify capability, and then
    release the maintenance lock
→ reopen capture acceptance at the Avibe sidecar intake on rotation-
    success terminal: the durable capture queue resumes receiving rows
    and provider calls resume; the rotation replay store continues to
    be drained until empty
→ on rotation-failure terminal: post-snapshot failure with verified
    snapshot restore completed ⇒ release the maintenance lock, restart
    the old sidecar against the restored provider root, verify capability,
    then reopen capture acceptance
→ retain the maintenance lock when the snapshot restore itself fails:
    the rotation journal records `restore_failed` at the failing phase,
    the lock is held, capture intake stays paused, and the next action
    is either another restore attempt by an operator or a fenced
    `clear` (see §15 acceptance); the lock is never released with the
    old sidecar offline
```

Holding the maintenance lock or pausing capture acceptance past the safe terminal state would lock the runtime indefinitely. The terminal transitions above are what make the rotation sequence actually return the runtime to a usable state.

Until EverOS provides a complete rotation operation, Avibe keeps the embedding-change guard over existing data and must not falsely report a LanceDB-only rebuild as a full migration. The "old generation stays intact on failure" claim only holds when both (a) EverOS was stopped and capture acceptance paused before snapshotting and (b) the snapshot restore actually completed. An operation journal alone cannot undo already-mutated derived state, and a snapshot taken while EverOS or capture acceptance is still live can omit captures that arrived mid-snapshot.

#### 8.3.1 Rotation Replay Store

The rotation replay store is a separate SQLite database outside `state/memory/memory.sqlite` (`core/memory/store.py:24`), so a snapshot of the durable capture queue never covers quarantined rows. The store lives at `state/memory/rotation_replay.sqlite` and is excluded from the rotation snapshot by the snapshot tool (it does not appear in the file manifest, the checksum pass, or the `sqlite3 backup` walk). It carries the same row identity as `memory_capture_queue` (`source_message_digest`, `principal_id`, `project_ref`, `session_id`, `epoch`, `payload_text`, `payload_attachments`, `provider_timestamp_ms`) plus six rotation-specific columns: `replay_state ∈ {pending_rotation_replay, in_flight, delivered_pending_rotation_commit, manual_required, settled, unknown}`, the new sidecar's `add_request_id`, the journal `operation_id` of the rotation that produced it, and the audit pointer for any `manual_required` decision. The `in_flight` value is written **before** the replay `/add` is submitted to EverOS: the rotation tool updates `replay_state = in_flight` and persists the `add_request_id` in the same SQLite transaction that records the lease, and only then sends the request. If Avibe crashes after the request reaches EverOS but before the acknowledgement is persisted, the row remains `in_flight` (or `unknown` if the lease is older than the recorded lease timeout) and is recovered to `manual_required` on the next boot unless a provider receipt proves the write was not committed; boot recovery never auto-replays an `in_flight` row. The `unknown` value is reserved for rows whose lease expired without a clear outcome.

Lifecycle:

- A row enters the rotation replay store as part of the fenced quarantine move in §8.3: the replay-store copy is written and verified before the source row is removed from the snapshot-covered outbox. A crash between those steps may leave an idempotent duplicate in the live queue, but must never leave the row only in the database being restored. Replay acknowledgements transition rows to `delivered_pending_rotation_commit` in this store.
- The rotation replay store is **not** backed up by the rotation snapshot by design (the snapshot must not cover it). It is, however, included in the **runtime backup set** under the same consistency fence and SQLite snapshot semantics as `state/memory/memory.sqlite`: the backup tool takes a coordinated snapshot of both SQLite files (WAL flushed, file checksums, sqlite3 backup walk), and a backup that omits `state/memory/rotation_replay.sqlite` is recorded as an incomplete backup and refused as a restore candidate. A backup never includes only one of the two databases; restoring an apparently complete backup that does not also restore the replay store would lose accepted captures that were quarantined at backup time.
- The rotation replay store is **not** wiped by `restart` of the live capture queue. `clear` of the live queue, however, is a destructive operator action that explicitly removes every Avibe-owned local Memory data (`docs/plans/memory-plugin-system.md:290-305`) and is the only sanctioned path to remove quarantined rows. A fenced `clear` transaction removes rows from both the live capture queue and the rotation replay store atomically: a half-completed `clear` that deleted only the live queue would otherwise let boot recovery replay quarantined rows and recreate memory after the user selected **Clear all**, breaking the deletion guarantee and potentially retaining sensitive conversation content. The fence is the same per-root maintenance lock that powers rotation and the audited `operate` path; rotation rollback and operator `restart` are the only paths that may leave the replay store undeleted.
- On snapshot restoration (post-snapshot failure path), the rotation replay store stays on disk; the next rotation re-reads its `pending_rotation_replay` rows and replays them in original order against whichever sidecar is then live. `delivered_pending_rotation_commit` rows that were committed to a now-rolled-back provider root are demoted to `pending_rotation_replay` with the same `add_request_id` for audit only; the next `/add` re-attempts the delivery against the restored-or-rebuilt root.
- A replay `/add` whose response cannot be confirmed (timeout, transport reset, or sidecar accepted-but-silent after the request was sent) is **not** marked `pending_rotation_replay` for retry. §10.1 treats write timeouts as ambiguous outcomes that auto-replay may not silently resolve; replay would risk duplicating an already-accepted memory. Such a row is marked `manual_required` with the audit pointer (the journal `operation_id`, the new sidecar's `add_request_id`, and the timeout evidence), and the rotation replay store is excluded from the live-queue feed. The row stays in the replay store until an audited `manual_required` resolution records operator intent and transitions the row to a follow-on state: case rf1 (later confirmed acknowledged) transitions `replay_state` to `delivered_pending_rotation_commit` with the confirmed `add_request_id`, then normal success-settlement runs once the rotation journal commits; case rf2 (confirmed `not_committed`) demotes the row to `pending_rotation_replay` for the next rotation; case rf3 (refuse and dispose) removes the row and records the operator decision in the audit log. The embedding fingerprint is not modified by any of these transitions — rf1 does not "pin a confirmed-acked fingerprint" because the fingerprint was already written in step 5 of §8.3 before any replay was attempted, and changing it would invalidate the new root's vector space. Boot recovery never replays a `manual_required` row.
- On rotation success, the rotation tool drains the replay store: rows that committed through the normal success-settlement path are removed, rows that did not commit (rare; only when the success-settlement path itself rejected them) return to `pending_rotation_replay` and stay in the store for the next rotation. `manual_required` rows do not return to `pending_rotation_replay` automatically; they wait for the audited resolution.
- On rotation-failure terminal (snapshot restore verified), the rotation tool **actively drains** `pending_rotation_replay` rows against the restored old sidecar before reopening capture acceptance, in the same fenced transaction that restarts the old sidecar and reopens intake. Reopening capture acceptance while quarantined rows are still waiting on the next rotation would let newer captures proceed against the restored old sidecar while previously-accepted captures from the same human user sit undelivered indefinitely, violating the "accepted capture is delivered, dropped, or held in a recoverable state" invariant and leaving the user-visible queue shorter than the actual pending state. The drain attempts each row against the restored sidecar in original order; rows that fail to deliver stay in the store as `pending_rotation_replay` (and surface as recoverable on the next rotation or operator action); rows that deliver transition to normal success settlement. The drain runs under the per-root maintenance lock; capture acceptance stays paused for the duration; only after the drain completes does the lock release and intake reopen. This makes "rotation failure" a true rollback to the pre-rotation state: no quarantined rows survive past the terminal transition.
- The store's own retention invariant is that **only `pending_rotation_replay` rows are boot-replayed**. The boot recovery path reads `pending_rotation_replay` rows from the store and feeds them back into the live capture queue before any new capture is accepted. `in_flight` rows are recovered to `manual_required` (the lease has expired without an ack and we cannot prove the write was not committed); `delivered_pending_rotation_commit` rows are not boot-replayed (they have already received an ack from the new sidecar and are awaiting rotation journal commit); `manual_required` rows are not boot-replayed (auto-replay is forbidden by the no-replay contract in §10.1); `unknown` rows are recovered to `manual_required` for the same reason; `settled` rows are not boot-replayed (they have been committed through the normal success-settlement path and were supposed to have been removed from the store, so a residual `settled` row is a fault indicator logged and surfaced on the processing-record page). Rows in any state other than `pending_rotation_replay` survive across boots but stay out of the live-queue feed.

This separation is what makes the round-5 claim — "quarantined rows survive snapshot restoration" — actually true. Without a separate store, the same SQLite is both the durable home for the row and the database the snapshot restores over, so restoring the snapshot is indistinguishable from deleting the row.

### 8.4 Rotation Entry Point

Rotation is triggered from the Web UI; CLI no longer exposes a standalone rotate command. The UI entry shape:

- Collapsed by default; only visible in the advanced/dangerous region of Settings → Memory;
- Trigger conditions:

  1. When the `embedding_semantic_fingerprint` changes (e.g. user swaps embedding model or provider config), a "rebuild" button appears automatically with the source of the difference annotated;
  2. The user actively picks "rebuild index" in settings and must pass a second confirmation modal that lists the impact, downtime window, and the cluster-centroid recomputation note;
- After second confirmation, §8.3 takes over; on failure, keep the old generation and never enter a "half-rebuilt" intermediate state;
- No equivalent CLI entry point is exposed; operators who need direct access reuse the internal `operate` interface exposed by the Web UI flow, with no new CLI surface.

Server-enforceable authorization and confirmation contract (because the UI surface is reachable through the same HTTP endpoint group as trusted local and Avibe Cloud browsers — `vibe/ui_memory_routes.py:1-8` — and a literal "UI click" cannot be distinguished from a scripted request to the same endpoint):

- The rotation endpoint sits behind the existing `operate` server contract: it requires an authenticated principal whose effective capability set includes `memory.rotation.execute`; the route is mounted only on the trusted local listen address by default and is rejected on Avibe Cloud with `403 capability_unavailable` unless that capability is explicitly granted. **This capability check is the actual server-side authorization boundary.** A scripted request that replays the second-confirmation modal cannot satisfy it: the principal must already be authenticated and authorized for `memory.rotation.execute` to receive any token at all, and the rotation body is rejected with `403 capability_unavailable` when the authenticated principal lacks the capability regardless of token shape. The modal itself is a UX gate, not a security gate;
- The request body carries a server-issued `confirmation_token` minted only after the second confirmation modal: the modal posts a `prepare` request that returns the token, the actual `rebuild` request must echo the token in the same principal session within a 60-second TTL. Missing, mismatched, or expired tokens cause the rebuild to fail closed. **The token is CSRF/replay protection between the modal interaction and the `rebuild` body, not the human-confirmation proof itself.** Two consecutive HTTP requests are not a substitute for an authorization check; a token bound to one principal cannot be replayed by another because the second request's principal session must match the first, and the token's 60-second TTL caps the replay window;
- The endpoint is bound to a single in-flight rotation per provider root, enforced by the maintenance lock (§7.3) so no parallel scripted caller can bypass the per-call lock;
- Chat, agent, API, and remote-channel callers have no path to mint `confirmation_token`; the `prepare` endpoint itself sits behind the same capability check and only returns a token after the modal interaction, which is not exposed to chat/agent/API surface area. The capability check remains the boundary even if the modal flow is later changed or removed; a rotation request that arrived without a modal-supplied token but with a principal authorized for `memory.rotation.execute` is still rejected with `403 capability_unavailable`.

## 9. Four-Mode Search Design

### 9.1 Avibe-Owned Search Policy

Suggested definition:

```text
RecallPolicy
  mode: auto | keyword | vector | hybrid | agentic
  limit: 1..N
  max_results: 1..N (internal retrieval budget; agentic-only)
  freshness: eventual | bounded | session_overlay
  wait_scope: trusted_provider_session_ref | none (required when freshness=bounded)
  target_generation: int | none (required when freshness=bounded)
  target_watermark_ms: int | none (required when freshness=bounded)
  freshness_timeout_seconds: int | none (required when freshness=bounded;
      bounds the §9.1 wait; not interchangeable with the agentic
      timeout below; missing or zero is rejected)
  include_profile: bool
  filters: provider-neutral filter tree
  -- agentic-only budgets (required when mode=agentic) --
  timeout_seconds: int (required, missing or zero is rejected; bounds the
      LLM step, not the freshness wait)
  max_model_calls: int (required, missing or zero is rejected)
  cost_budget_tokens: int (required, missing or zero is rejected; large
      values are accepted as effectively unlimited but must be declared)
  -- multi-declaration support (see §9.2.1) --
  declarations: list<RecallDeclaration> | none
      (an explicit, ordered list of additional independently-budgeted
       mode runs that share the same caller-facing `limit` and
       `freshness` contract; empty or omitted means single-mode run)
```

```text
RecallDeclaration
  mode: keyword | vector | hybrid | agentic
  budget: RecallBudget
```

```text
RecallBudget
  limit: 1..N (declaration-local upper bound; must satisfy
      declaration_limit <= caller_limit)
  max_results: 1..N (agentic-only)
  freshness_timeout_seconds: int | none (required when declaration uses
      freshness=bounded)
  timeout_seconds: int | none (agentic-only LLM step budget)
  max_model_calls: int | none (agentic-only)
  cost_budget_tokens: int | none (agentic-only; missing or zero
      rejected when the declaration uses mode=agentic)
```
```

`limit` and `max_results` are two distinct fields with a defined relationship:

- `limit` is an **upper bound**, not a required count. It caps how many items the adapter will return to the caller, regardless of mode. A successful query whose underlying store holds fewer than `limit` matching records — including an empty memory store — returns the available short result with the ordinary success outcome. The adapter does not pad, fabricate, or repeat rows to reach `limit`, and it does not report `timeout` or `capability_unavailable` solely because the candidate count is short.
- `max_results` is an internal retrieval budget on how many candidates the adapter is allowed to pull from the search backend while satisfying the request; it exists so an `agentic` caller can spend retrieval work without inflating the response size or the LLM input budget. `max_results` is only meaningful when `mode=agentic`; for `keyword|vector|hybrid|auto` it is rejected at the adapter layer.
- The adapter validates `max_results >= limit` and rejects the request when the relationship is violated. The relationship is one-way: `max_results` may be strictly greater than `limit` (extra candidates feed the reranker and the LLM), but `max_results < limit` is rejected because it cannot satisfy the upper-bound contract on either side.
- The adapter never silently returns more than `limit` items to the caller, never silently downgrades an honest short result into a timeout or capability-unavailable outcome, and never pads results with non-candidate rows to reach `limit`. The same field, the same validation rule, and the same short-result semantics apply regardless of whether the call site is the UI, the CLI, or another backend module.

- `mode=auto` only chooses among `keyword/vector/hybrid` and must never implicitly escalate to `agentic`;
- `mode=keyword/vector/hybrid/agentic` are explicit choices; the caller is responsible for declaring budgets;
- `freshness=eventual` does not promise a deadline; `bounded` is best effort within the caller's `freshness_timeout_seconds` deadline and returns explicit `timeout` / `unknown` on timeout; `session_overlay` binds only to a trusted current session. The `freshness_timeout_seconds` field is independent of the agentic-only `timeout_seconds` budget: a `keyword` / `vector` / `hybrid` caller that uses `freshness=bounded` cannot borrow the agentic `timeout_seconds` because that field is rejected for non-agentic modes, and an `agentic` caller with `freshness=bounded` declares both deadlines (the freshness deadline bounds the §9.1 wait, the agentic `timeout_seconds` bounds the LLM step after the wait resolves). The deadline the adapter applies to a bounded wait is `freshness_timeout_seconds`; when omitted or zero, the adapter rejects the request at the boundary with the same fail-closed rule as the agentic budget fields.
- `freshness=bounded` requires three additional fields so the adapter can implement the §14 success criterion against the right session: `wait_scope` must be the trusted provider session reference (`principal_id`, `epoch`, `project_ref`, `session_id` per §5.3), `target_generation` must name the generation that bounded recall is waiting on, and `target_watermark_ms` must be the watermark the adapter must observe before returning success. The generic `filters` tree is **not** an acceptable substitute for these fields — a filter cannot identify a session. A `bounded` request that omits any of `wait_scope` / `target_generation` / `target_watermark_ms` is rejected at the adapter layer with the same fail-closed rule as the agentic budget fields; success defined as "flush confirmed successful" (§14) cannot be implemented without naming the generation whose flush must confirm. The observation source for the target generation / watermark / flush_state is the **Avibe-owned coordinator's durable state** (`memory_meta` plus the per-session durable generation / watermark / flush_state columns), not EverOS: the sidecar's `core/memory/sidecar.py:177-200` allowlist exposes only `/health`, `/add`, `/flush`, `/search`, and `/get`, and `MemoryRuntime.status_payload()` (`core/memory/runtime.py:542-558`) is an aggregate without per-session generations or watermarks. The plan does not modify EverOS, so a per-session `/status` projection is out of scope for this plan. The adapter observes through the same coordinator-owned Avibe durable state that the capture path already writes; the session fence that the capture path holds is the same fence the bounded wait observes under; on every poll, the adapter reads the target session's `generation`, `watermark`, and `flush_state` from coordinator-owned state, and returns success only when **all three** of the following are true: (a) the live `generation` has reached or passed `target_generation`; (b) the live `watermark` has reached `target_watermark_ms`; (c) the live `flush_state` for that session is `settled`. A `flush_state` of `due` / `in_flight` / `unknown` / `manual_required` is not a successful freshness result even when generation and watermark look right — §14 success is a confirmed flush, not a watermark the provider has merely acknowledged. When the deadline elapses with any of generation / watermark / flush_state below the required threshold, the adapter returns `timeout`; when an unresolvable error blocks observation, it returns `unknown`. The same three-way predicate applies regardless of mode, including `agentic` (which observes bounded freshness on the same coordinator state before its LLM step).

EverOS DTOs, filters DSL, and response arrays live only inside the adapter.

### 9.2 Mode Selection

| Mode | Use case | Capability requirement | Default-ness |
|---|---|---|---|
| `keyword` | Precise names, IDs, error codes, commands, terms | BM25 / index | Fallback when embedding is unavailable |
| `vector` | Paraphrase, semantic similarity, multilingual | embedding | Explicit / policy choice |
| `hybrid` | General recall, balancing precision and semantics | embedding + BM25 | Default |
| `agentic` | Multi-hop, complex reasoning, multiple retrievals | LLM + embedding + reranker | Explicit opt-in |

This adjustment adopts both the reranker configuration and explicit `agentic` search. The extra LLM / reranker latency and call cost is accepted as a product-level semantic cost.

#### 9.2.1 Agentic Default and Trigger Policy

- `auto` does not implicitly choose `agentic`; only `keyword/vector/hybrid` are reachable via `auto`;
- Ordinary UI search does not call `agentic`;
- Ordinary chat / system recall does not call `agentic`;
- `agentic` is only enabled by an explicit caller declaration and must pass Avibe-owned policy validation;
- A single `RecallPolicy` body may carry multiple independently-budgeted mode runs through the explicit `declarations: list<RecallDeclaration>` field. The top-level scalar `mode` is the primary run and remains the contract for the caller-facing response; each entry in `declarations` is an additional independently-budgeted run that shares the same `limit` and `freshness` contract but has its own `RecallBudget`. JSON duplicate-key encoding is **not** used and would be silently dropped by ordinary parsers, so the multi-run shape is modeled only through the typed list field. The adapter executes declarations in declared order, each under its own budget; the caller sees the primary run's response and the declarations' results are surfaced only through their own metadata projection (no implicit merging, no implicit fallback between declarations). Each declaration's `limit` must satisfy `declaration_limit <= caller_limit`; an `agentic` declaration must carry the full §9.2.2 budget set; a declaration that uses `freshness=bounded` must carry `freshness_timeout_seconds`. The total wall-clock budget of the request is the sum of declaration budgets plus the primary run's budget; the adapter rejects a request whose total exceeds the caller's process-level timeout. A request with no `declarations` field is a single-mode run; the existing scalar `mode` is the source of truth.

#### 9.2.2 Agentic Required Budgets

The Avibe-owned `RecallPolicy` must carry, when `agentic` is allowed:

- `timeout_seconds`: required, declared explicitly by the caller; no implicit default; missing or zero is rejected;
- `max_model_calls`: required, declared explicitly by the caller; no implicit default; missing or zero is rejected;
- `max_results`: required when `agentic` is allowed, declared explicitly by the caller; no implicit default; missing or zero is rejected; relationship to `limit` is fixed by §9.1 (`max_results >= limit`);
- `cost_budget_tokens`: required when `agentic` is allowed; declared explicitly by the caller; no implicit default; missing or zero is rejected (the value may be a large upper bound so that it is effectively unlimited, but the caller must declare it). On exceed, return `capability_unavailable` immediately. The plan therefore treats `cost_budget_tokens` as part of the agentic completeness contract, not as an optional field.

On timeout or capability failure, return an explicit result (`timeout` or `capability_unavailable`) — never mask as a successful hybrid. The plan therefore does not expose a `allow_fallback_to_hybrid` switch on the Avibe-owned `RecallPolicy`; §9.2.4 forbids fallback and §14 requires `capability-unavailable` without fallback. A caller that needs hybrid must declare `mode=hybrid` directly.

Single contract: any missing or zero budget field causes the adapter to fail closed and reject the request before forwarding to EverOS. The plan does not introduce Avibe client-side default budget numbers and does not silently forward to EverOS defaults, so behavior is the same whether the call site is the UI, the CLI, or another backend module. Any later adjustment must first express the budget field at the call site and keep the fail-closed rule intact.

Agentic requests that do not carry the complete budget fail closed at the adapter layer.

#### 9.2.3 Reranker Configuration

New Avibe configuration:

```text
memory:
  processing:
    reranker:
      enabled: true
      base_url: https://...
      model: ...
      api_key: ...
      timeout_seconds: 20
      max_concurrent: 4
```

Behavior:

- The API key enters only the managed EverOS child environment, the same as LLM and embedding;
- API-key change triggers a sidecar restart only and does not enter the semantic embedding fingerprint;
- Model change records a `reranker_config_fingerprint` only and does not rebuild LanceDB;
- When missing or disabled, the `agentic` capability is unavailable;
- UI/API responses expose only `enabled`, `configured`, and `available`; never the key or secret;
- The `/health` summary's capabilities list grows to include `reranker`, so the processing-record page and inspection scripts can locate it;
- The processing-record page shows capability status only, never vendor configuration.

#### 9.2.4 Capability Gating

When a request with `mode=agentic` arrives:

- LLM, embedding, and reranker must all be available before allowing it through;
- If any one is unavailable, return `capability_unavailable` with the missing provider named;
- No fallback to `hybrid`;
- No implicit trigger via agentic configuration;
- The request body only accepts methods on the allowlist; anything else is rejected by the sidecar route guard — this is an independent gate from capability gating.

#### 9.2.5 Privacy and Logging

- `agentic` search follows the existing provider-call recorder policy of metadata-only logging — **not** the full-payload path. The default `normalize_provider_call()` (`core/memory/everos_insight/recorder.py:480-572`) serializes the normalized request and response into `request_json` / `response_json`, and `_llm_request()` bounds message content without redacting it; that path is not safe for agentic search because the LLM call carries the user's query and the retrieved memory context. Agentic search therefore uses an explicit metadata-only recorder path: a separate `record_agentic_metadata()` entry point that records only `kind=agentic_llm`, `mode`, `stage`, `model`, `status`, `duration_ms`, `prompt_tokens`, `completion_tokens`, `request_id`, `strategy_name`, `run_id`, `attempt`, `memcell_id`, `app_id`, `project_id`, `owner_id`, `md_path`, `entry_id`, `parent_type`, `parent_id`, `dropped_before`, and a scrubbed `error`. The `request_json` and `response_json` columns are written as `null` for agentic calls, with the prompt and response bytes reflected only in `request_bytes` / `response_bytes` aggregate counters. The same `_MAX_ROW_ENCODED_BYTES` budget guard applies. Contract tests assert that an agentic LLM call with a recognizable prompt substring and a recognizable retrieved-memcell substring produces a recorder row whose `request_json IS NULL` and `response_json IS NULL` and whose `request_bytes` / `response_bytes` are non-zero; the substrings must not appear in any serialized column. The existing provider-call detail projection (§11.4) continues to body-redact any non-agentic call that does retain content;
- Agent chat memory capture is out of scope; do not enable agent case / agent skill;
- User-memory `agentic` only returns visible records to the caller; it must not leak into other users' profiles.

#### 9.2.6 Out-of-Scope (Explicit Non-Goals)

- Do not enable assistant/tool/agent capture;
- Do not switch Memory capture provenance;
- Do not change owner scope;
- Do not modify EverOS source code;
- Do not promise `agentic` capability always available before EverOS upstream supports it.

### 9.3 Profile and Recent Messages

- Profile is decided as user-global (see §14). Phase 3 schedules the owner-keyed `/get(memory_type="profile")` adapter path; the search-literal `"profile"` workaround remains as a fallback for hosts that prefer it. The project-isolation limitation is no longer a precondition because profile scope is user-global;
- Relevance queries use `/search`;
- The current session's unprocessed messages use the EverOS `unprocessed_messages` overlay;
- Results distinguish `source=unprocessed` from `source=extracted`;
- Unprocessed messages must not be mis-labeled as already-extracted long-term memory.
- `eventual` does not promise a deadline; `bounded` is best effort within the caller's deadline, must return explicit timeout/unknown, and must not claim completion; `session_overlay` only accepts a trusted current session and must surface the data source and partial state.
- The overlay does not cover messages still in the Avibe outbox, nor Markdown written but not yet Cascade-projected; if read-your-write is required, define a target watermark and wait scope explicitly.

## 10. Errors, Concurrency, and Security

### 10.1 Error Classification

The adapter must classify errors primarily by EverOS `error.code`:

- Network error, timeout, `EXTERNAL_SERVICE_UNAVAILABLE`: classify into **pre-submission** failures (connection refused before the request body was written, TLS handshake failed, DNS resolution failed, setup timeout before the write began) versus **post-submission** transport failures (write succeeded but the response was lost — connection reset, read timeout after the request body was fully written, half-closed socket after the request body). Pre-submission failures are bounded exponential-backoff retry; post-submission failures are **not** retryable and route to `unknown` / `manual_required` exactly like write timeouts, because the write may have already reached EverOS and a retry could duplicate an accepted message or duplicate extraction work;
- `INVALID_INPUT`, `BAD_REQUEST`, `UNSUPPORTED_FORMAT`: do not retry;
- `PROVIDER_NOT_CONFIGURED`: do not retry until the configuration is fixed;
- `CAPABILITY_UNAVAILABLE`: permanent capability gap, do not blindly retry as if it were HTTP 503;
- Unknown add/flush outcome: record as `unknown`, treat as "may have committed"; without receipt/reconciliation or a proven `not_committed`, do not auto-replay; transition to `manual_required` and do not claim extraction success;
- **Write timeouts on `/add` or `/flush`** are treated as ambiguous outcomes, not as retryable network errors. Once the request may have reached EverOS but no response was confirmed, retries are forbidden; the call falls through to the `unknown` rule above. Only failures proven to occur before submission, or read-side timeouts, are retry-eligible. This avoids duplicate accepted messages or duplicate extraction work, and keeps the `unknown`/`manual_required` rule consistent.

The current `EverOSPort` still relies heavily on HTTP status; that should be consolidated later.

### 10.2 Concurrency Rules

- `add`/`flush` for the same provider session reference (`principal_id`, `epoch`, `project_ref`, `session_id`) are serial; `app` is recorded for diagnostics but does not split the serialization domain;
- Different sessions may run concurrently;
- One provider root permits only one EverOS process;
- `clear`, `rebuild`, `rotation`, and `artifact cutover` are mutually exclusive;
- No SQLite transaction may span HTTP / LLM / model calls;
- The maintenance lease must be acquired before the session lease, with a fixed lock order.

### 10.3 Security Boundary

Continue to enforce:

- owner-only UDS;
- exact route / shape validation;
- HMAC-derived principal/project; never store raw platform IDs or working paths as provider identity;
- attachment-root containment and symlink checks;
- child-environment allowlist;
- response body, item count, nesting depth, and string length limits;
- never expose raw message bodies, secrets, user IDs, or high-cardinality session labels.

Provider-call log is the processing-record page's diagnostic data source, not a general-purpose metric system. It is retained because today's UI log and provenance reads depend on it, but it must not be coupled to the new global status machine.

## 11. Improvements to the Existing Processing-Record Page

The existing log page's core display logic is sound: memcell-centric, showing capture, add/flush, OME processing, provider calls, and index linkage. It should shift from "Memory status console" to "processing record and diagnostic page" without being rebuilt.

### 11.1 Retained Core Logic

Keep:

- memcell list and pagination;
- memcell preview, time, and message count;
- the detail page's precise capture → memcell → OME run → provider-call linkage;
- scope / principal / project access control;
- provider-call bounded, scrubbed request/response expansion;
- fail-closed notice for missing, stale, truncated, and unavailable data;
- count, byte-size, and nesting-depth limits on list and detail.

These answer "how was this memory processed" and align with the new page's positioning.

### 11.2 Distinguishing the Four Data Semantics

The page and backend types must distinguish:

| Data semantic | Example | Display rule |
|---|---|---|
| Historical processing fact | memcell, capture, precisely linked run/call | Goes into the historical timeline |
| Data-source availability | Whether EverOS DB, capture queue, and call log are readable | Label as `Source availability`, not "system health" |
| Current snapshot | Current profile, current indexing row, current error | Show separately as `Current snapshot`; never put into the historical timeline |
| Derived or incomplete data | Run aggregation, profile trigger relations, stale call log | Mark as `inferred/expired/omitted`; never treat as complete events |

Today `current_state` should be renamed to `Current snapshot` and visually separated from the historical steps.

Today `sections` should be renamed to `Source availability` and only express whether the relevant data sources are readable; `partial` must not map to a global `Memory degraded`.

### 11.3 List-Page Activity Summary

Today's `run_summary` is derived from OME `run_record` aggregation, not a complete lifecycle fact. Recommendation:

- The list page only keeps a compact "processing activity" summary;
- Do not show complex run-status combinations;
- Detailed run status lives on the memcell detail page;
- `authorized_call_count` is renamed to "Recorded calls" or "Linked calls" to avoid implying full provider-call coverage.

### 11.4 Provider-Call Display Boundary

Provider-call detail is kept, but the wording must be "recorded provider calls", not "all calls":

- When the recorder is degraded, warn that some calls may be missing;
- When the call log is stale, show `expired`;
- Provider payload continues to show only scrubbed, bounded fields;
- Continue to forbid raw sidecar stdout/stderr, attachment bytes, embedding vectors, and unprocessed secrets;
- Copy operations only copy projected and scrubbed content.

For operator-facing call-detail panels the projection must explicitly redact the conversation body (prompt text, response text, tool result text). Secret and path scrubbing plus byte bounds do not remove the message body, and this plan's security invariant requires that bodies are never exposed via the page or any future Avibe Cloud surface. The body-redacted projection is what the page renders; the underlying recorder may keep the body in its private store for debugging but must project through a body-redacted view before returning to the page or any UI. Existing `core/memory/everos_insight/reader.py` must therefore expose two views: a full view (internal-only, never returned to UI) and a body-redacted view (returned to the page and any external consumer).

The same rule applies to the **memcell preview** rendered in the processing-record list. `_memcell_preview` (`core/memory/everos_insight/reader.py:1289-1328`) currently projects raw user text after only secret / path scrubbing, and `memory_admin_log_access` is granted to every verified UI key (`core/internal_server.py:995-1003`), which means a Cloud browser session opening this page can read conversation text that does not belong to that session's principal. The list-read path therefore must satisfy both invariants:

1. **Body-redacted preview.** The list-read view is a body-redacted projection. The recorder may keep `text` / `content` / `tool result text` in its private store, but the row returned to the page must replace any conversation body with the same `[redacted]` marker used by the provider-call detail projection; metadata that is not a conversation body (timestamps, message-id, role, sender-id, item-count, capability tags, provider-call links) stays visible so the row remains usable as a processing record.
2. **Principal-scoped list reads.** The list endpoint accepts a `principal_id` filter, defaults the filter to the principal derived from the requesting principal's verified UI key, and refuses to render a memcell whose `sender_ids_json` does not contain that principal. The full internal view (un-redacted, all principals) is reachable only through an explicit admin capability that is not granted by `memory_admin_log_access`; the Cloud browser session therefore cannot enumerate memcells belonging to other principals even if it bypasses the redacted projection. The default is derived through the same canonical helper that all capture paths already use: `runtime.principal_for_user_key()` (`core/memory/store.py:208`), whose underlying `derive_principal_id` HMACs the literal input under the per-root `scope_key` (`core/memory/store.py:1490-1498`). HMAC over a fixed key is one-way and collision-resistant, so **distinct literal inputs yield distinct `u-…` principals**: `avibe:local` produces one principal, `avibe:remote:<subject>` produces another, and `slack:<user_id>` / `discord:<user_id>` / `telegram:<user_id>` / `feishu:<user_id>` / `wechat:<user_id>` each produce their own principals. The browser principal and the IM principal of the same human user are **not** the same string, and the round-7 wording that claimed they were is wrong. The list-read path therefore enumerates the **principal set** that the requesting browser principal owns, not a single principal string:

   - the requesting browser's own principal, derived via `principal_for_user_key` of the verified UI key;
   - every IM principal whose `platform:user_id` literal was previously bound to that same browser principal through the verified alias map below.

   The alias map is **explicit and audited**, not a free-form HMAC equality claim. When a Cloud browser session is paired with an IM account (Slack / Discord / Telegram / Feishu / WeChat) under the Web UI's pairing flow, the pairing records an audited alias row of the form `(browser_principal_id, platform, im_principal_id, paired_at, audit_pointer, verification_evidence_ref)` in `memory_meta` (or a sibling alias table). The row stores the **already-derived IM principal** — `principal_for_user_key(f"{platform}:{im_user_id}")` — not the raw `im_user_id`. The raw `im_user_id` is held only in the audit_pointer / `verification_evidence_ref`, which is a scrubbed evidence pointer (challenge nonce, response digest, and platform user-id hash) produced by the platform-side ownership challenge, never the literal user-id string. Storing the raw `im_user_id` directly in the alias row would contradict §10.3's HMAC-derived identity rule and the existing `derive_principal_id()` design (`core/memory/store.py:1490-1498`) that deliberately avoids retaining the platform user key in durable state; runtime backups would acquire stable raw Slack / Discord / Telegram / Feishu / WeChat identifiers. The pairing flow must include a **platform-side ownership challenge** before the alias row is recorded: the human at the browser must prove control of the IM account through the platform's identity primitive (Slack / Discord / Telegram / Feishu / WeChat's verification flow) and the verification response is recorded as the `verification_evidence_ref`. Without that challenge, an attacker who learns a victim's IM user-id could forge an alias and authorize themselves to read the victim's memcells; the challenge is the ownership boundary. Each alias row also carries a uniqueness rule (one browser principal can be paired to one IM account per platform per session; re-pairing revokes the prior alias) and a revocation rule (the alias can be revoked by either side; revocation removes the alias row and any subsequent list-read filter no longer includes the IM principal). The list-read path resolves the browser principal against the alias map to compute the principal set, then matches against `sender_ids_json` membership across that set. The full alias map is exposed only through the same admin capability as the full internal view; an unprivileged Cloud browser session sees only its own aliases. The default then narrows by the project scope already resolved for the request through `_memory_read_scope()`. Without this alias map and challenge, the browser list-read filter would reject every ordinary IM memcell; with them, IM captures from the same human user are surfaced, and the ownership check remains exact.

Both invariants are checked at the adapter boundary before any row leaves `core/memory/everos_insight/reader.py`. The page renders body-redacted, principal-scoped rows only; the internal recorder is the only consumer of the full view.

### 11.5 Target Processing-Record Page Structure

```text
Processing record

┌────────────────────────────────────┐
│ EverOS runtime summary             │
│ version · capabilities · cascade   │
│ recorder · observed_at             │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│ Source availability                │
│ EverOS · capture · call log        │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│ Memcell list                       │
│ time · preview · message count · activity │
└────────────────────────────────────┘

Detail:
  Memcell overview
  ├── Historical pipeline
  ├── Recorded provider calls
  ├── Current snapshot
  └── Missing/stale/truncated notes
```

The top EverOS runtime summary only lightly reads the existing `/health`; it does not introduce a new EverOS interface and does not expose the full internal outbox/lifecycle state tree.

### 11.6 Misleading Wording to Delete

Do not derive global state from:

- a single memcell's successful run;
- linked/recorded call counts;
- zero Cascade pending;
- a recent `/health` success;
- the existence of a profile file.

Each of these can only prove its own single fact; it cannot prove "all messages processed", "all OME done", or "all indexing converged".

### 11.7 EverOS Private Schema Dependency

The existing `core/memory/everos_insight/reader.py` directly reads the pinned EverOS `system.db`, `ome.db`, `md_change_state`, memcell, and provider-call provenance. This log adapter may stay, but it must:

- declare itself a pinned-runtime adapter, not a stable EverOS public API;
- run real-wheel contract tests on every EverOS upgrade;
- fail closed to `unavailable/partial` on schema or field mismatch; never return fabricated success;
- centralize message-id derivation, run-event fields, and owner/scope relations inside the adapter;
- not expand private-read logic into a global status machine.

### 11.8 Processing-Record Page Acceptance Focus

- List and detail can show precise, verifiable processing facts around memcells;
- Current snapshot is visually, type-wise, and text-wise separated from the historical timeline;
- `Source availability` is not interpreted as global Memory health;
- Provider calls are explicitly labeled as recorded, possibly stale, or possibly missing;
- `run_summary` stays compact in the list and can be expanded in detail;
- The EverOS `/health` summary carries observation time and lists capabilities: LLM, embedding, reranker, parser, agentic;
- Private schema read failure renders as `unavailable/partial`, never as `ready/degraded`;
- Existing permission, scrub, pagination, and response-boundary rules continue to hold.

## 12. Phased Implementation Plan

### Phase 0: Close Reliability and Compatibility Contracts First

This phase does not require EverOS 1.2.3 to be in place. Define and test the Avibe-side contract first:

- `unknown` provider outcome → `manual_required`, no auto-replay;
- Final flush acquires the session fence before drain check;
- Canonical provider session reference (`principal_id`, `epoch`, `project_ref`, `session_id`), generation, watermark, fence epoch;
- Idle / max-age durable `due`, boot recovery, batched backoff;
- Attachment acceptance, private copy, backup scope;
- Profile scope (decided user-global), session overlay authorization, freshness semantics;
- Existing status payload, `data_exists`, CLI machine-readable fields stay compatible;
- Define the processing-record page's data boundary and pinned-runtime adapter contract;
- Audited manual resolution operation for `manual_required` per §5.5.

EverOS 1.2.3 runtime artifact, manifest/checksum, and compatibility verification is an independent release track; it can proceed in parallel and does not block stopping per-message flush or simplifying the processing-record page.

### Phase 1: Merge the Status Page and Processing-Record Page

- Remove the user-visible narrative of the standalone Memory status page; keep the existing `/status`, `data_exists`, CLI, and internal machine-readable contract;
- Read the existing EverOS `/health` summary at the top of the processing-record page;
- Show EverOS version, capabilities, Cascade summary, recorder state, and observation time;
- Keep the existing processing log, anomalies, and recovery records;
- Do not modify EverOS; do not add a business-metrics protocol;
- Delete user-visible composite status derivation, provider probes, and provider-root deep scans; do not delete the internal checks required by the drain/embedding safety gate;
- Keep the internal outbox/lifecycle delivery and child supervision capability, but do not expose its full counter tree to the page;
- Old status fields stay compatible first; new source/observed_at/unknown information is added additively.

### Phase 2: Session Flush Coordinator

- Add durable session flush state;
- Remove the post-add flush;
- Add idle, max-age, and explicit-close flush;
- Wire `/new`, archive, and session close;
- Add boot recovery and unknown fencing;
- Add same-session generation / concurrency tests.

### Phase 3: Search Policy Expansion

- Add reranker configuration and the Avibe-owned config write;
- Open the sidecar allowlist to validate `keyword/vector/hybrid/agentic` methods;
- Adapter maps Avibe-owned `RecallPolicy`, carrying `timeout/max_model_calls/max_results/cost_budget_tokens`;
- `hybrid` is the default; `keyword` is the fallback; `agentic` is explicit only;
- All three capabilities (LLM/embedding/reranker) must be available before `agentic` is allowed; otherwise return `capability_unavailable`, no fallback;
- **Mode-specific capability gating.** Activation and read capability are checked per mode at the shared runtime layer (`runtime.py:340-372` for `processing_healthy`, `process.py:219-223` for the start gate), so `keyword` recall is reachable when only the embedding endpoint is missing — the existing shared gates that require *both* LLM and embedding settings (`process.py:1144-1153` and the `_processing_configured()` AND-chain in `everos.py:340-372`) are split into mode-specific predicates. Specifically: `_keyword_runtime_ready()` checks only that the sidecar is alive and `/search` accepts `keyword`; `_vector_runtime_ready()` adds the embedding endpoint; `_agentic_runtime_ready()` adds the reranker + LLM + budget fields. The `processing_healthy` aggregate is replaced by per-mode readiness signals, and the page surfaces each mode's actual readiness rather than a single composite. A `keyword` request that hits an unavailable embedding endpoint must not be rejected at the runtime gate; it must reach the adapter, which then returns the keyword result against the FTS/scalar index without ever consulting the embedding endpoint. This change does not modify EverOS and does not weaken any existing LLM-only or embedding-only gate that protects `agentic` or `vector` modes;
- Profile switching to `/get` is no longer gated on a project-isolation gap because the profile scope is user-global (§14). The Phase 3 task is to add the owner-keyed `/get(memory_type="profile")` adapter path while keeping the search-literal fallback for hosts that prefer it, and to test the user-global contract end-to-end. Any future reopen of project-scoped profile is a separate decision with its own upstream dependency.
- Add the unprocessed overlay bound to a trusted current session;
- Add scope isolation, filters, overlay bypass, freshness, agentic budget, and capability-boundary tests;
- Processing-record page's capabilities grow to include `reranker`; agentic search continues under the existing provider-call recorder policy of metadata-only logging (see §9.2.5).

### Phase 4: Avibe-Side Recovery Boundary and Fault Injection

This phase does not modify EverOS and does not promise receipt/replay capabilities EverOS does not yet have:

- `unknown` add/flush without receipt stays at `manual_required`, no auto-replay;
- Ship the audited manual resolution operation described in §5.5 with journal-backed audit rows;
- Make explicit the security prerequisites for payload scrub, attachment retention, and durable-home transfer;
- Inject faults for Avibe enqueue, add timeout, flush timeout, sidecar crash, and controller restart;
- For the crash window after EverOS boundary but before Markdown, verify only fail-closed visibility, do not fabricate an exactly-once fix;
- Promote Avibe-controlled durability invariants to contract tests;
- Caller stable identity, receipt lookup, and memcell-to-Markdown replay are recorded as future EverOS upstream capabilities.

### Phase 5: Embedding Rotation

- **Gate.** Full semantic rotation ships only when one of the following is true: (a) the pinned EverOS exposes a complete rotation operation covering LanceDB rebuild, cluster centroid recompute, embedding-dependent OME invalidation, and fingerprint write; or (b) Avibe ships an Avibe-owned equivalent that performs all those steps under the maintenance lock and operation journal below. Until one of those holds, the user-visible rotation button (§8.4) stays hidden and only key-only restart is exposed; the current fail-closed behavior over existing data is retained.
- Ship key-only restart first;
- Then ship the offline full semantic rotation under the gate above;
- Cover LanceDB, cluster, OME, and embedding-dependent Markdown;
- Use the maintenance lock, operation journal (`operation_id/phase/fingerprint/fence_epoch/owner`), and rollback / fail closed;
- Old-epoch late writes must be rejected;
- Blue-green rebuild is evaluated later.

### Phase 6: Release and Verification

- Focused unit / contract tests;
- EverOS real-wheel contract tests;
- Runtime artifact checksum / architecture / version tests;
- UI build;
- User-visible behavior is verified with the local Incus regression environment; do not restart the local coding-agent `vibe` service.

## 13. Acceptance Criteria

### Flush

- Continuous messages do not trigger one flush each;
- Multiple messages from a single session can collapse into a natural memory cell;
- Idle / close / max-age triggers are reliable;
- Final flush acquires the session fence before checking drain, with the fence held through the flush;
- Generation uses a uniform provider session reference (`principal_id`, `epoch`, `project_ref`, `session_id`), watermark, and fence epoch;
- New messages during an in-flight flush are neither lost nor mixed into the wrong generation;
- `due`/`unknown` session state persists and resumes in batches after restart;
- `unknown` without receipt enters `manual_required` and is not auto-replayed;
- An `extracted` ack advances the watermark and recomputes the next generation's age from remaining messages;
- Shutdown does not block on LLM flush indefinitely.

### Processing-Record Page

- The standalone status page is removed; the status summary is folded into the processing-record page;
- The page only shows what `/health` and the existing processing log can directly confirm;
- Every runtime summary carries observation time; reads that fail or are stale render `unknown/unavailable`;
- `/health.status="ok"` is not promoted into "all Memory pipeline ready";
- Ordinary page reads do not trigger processing-endpoint probes or provider-root deep scans;
- The outbox/lifecycle full counter tree and internal state machine are not exposed as a user-visible dashboard;
- Bodies, secrets, and unrelated internal identifiers are never exposed;
- Recorder degraded / corrupt is still visible and recoverable on the processing-record page;
- Provider-call detail uses the body-redacted projection (see §11.4).

### Minimum Reliability

- Outbox keeps durable enqueue, idempotency, claim, bounded retry, success settlement, and boot recovery;
- Lifecycle keeps child start/stop, UDS, crash recovery, and maintenance operation fence;
- The outbox/lifecycle responsibility for global `ready/syncing/degraded` derivation is removed;
- After simplification, an accepted capture cannot be lost just because the provider is temporarily unavailable;
- A single provider root cannot be used by two managed EverOS children simultaneously.

### Data and Rebuild

- Accepted Avibe capture lives in at least one recoverable durable home;
- Attachment capture is only marked recoverable when the bytes sit in an Avibe-owned durable store and have been pinned by the pending capture, or the product explicitly accepts non-replayable semantics;
- Mis-deleting `.index` is not described as harmless by any doc or code;
- Projection rebuild does not delete `unprocessed_buffer` or memcell;
- After a LanceDB rebuild, the done queue does not produce an empty index;
- Embedding key-only change does not trigger a full rebuild;
- Semantic change does not mix two vector spaces;
- Rotation handles cluster centroid and embedding-dependent OME state;
- Rebuild / rotation operation journal can resume, roll back, or fail closed after a crash;
- Status payload, `data_exists`, and CLI machine-readable fields stay compatible after page simplification.

### Search

- `keyword/vector/hybrid/agentic` have real adapter contract tests;
- `agentic` with missing reranker/LLM/embedding returns `capability_unavailable` without fallback;
- `agentic` explicit timeout returns an explicit timeout, never masquerades as `hybrid`;
- `hybrid` is the default;
- `keyword` remains usable when embedding is unavailable;
- `agentic` returns `capability-unavailable` when reranker/budget configuration is missing, without implicit invocation;
- Profile `/get` target behavior is covered by tests under the user-global scope (§14); project isolation is not asserted and is not a precondition, because the profile scope decision is closed;
- Session overlay only accesses a trusted current session and rejects arbitrary session filters;
- user / project / session isolation tests pass (profile scope follows the decided user-global semantics);
- Unprocessed messages and extracted memory have explicit freshness, source, and watermark / partial markers.

### Manual Resolution

- `manual_required` is observable on the processing-record page (audit row + decision row);
- `restart` cannot be used to clear `manual_required`; the row stays `manual_required` and continues to require an audited resolution path (rf1/rf2/rf3);
- `clear` is a destructive operator action that explicitly removes every Avibe-owned local Memory data (`docs/plans/memory-plugin-system.md:290-305`) **and** removes `manual_required` rows from the rotation replay store under the per-root maintenance lock, in the same fenced transaction as the live-queue clear, with a deletion audit row recording the operator, the timestamp, and the count of `manual_required` rows removed. **This is destructive clear, distinct from "clear-as-evidence"**, which is forbidden: §5.5 case rf3 already records operator intent through the audited `operate` path, not through `clear`; a `clear` that silently treated every `manual_required` row as resolved would destroy the evidence chain that proves the operator saw and decided the row. The destructive `clear` is the only path that may remove `manual_required` rows without producing an rf1/rf2/rf3 decision row, and it must produce its own audit row;
- The audited `operate` path advances watermark and fence without mixing generations;
- Audit rows are durable and survive restart;
- Unrelated sessions are not blocked by a `manual_required` fence.

## 14. Decisions That Require Product Confirmation

Product semantics below must be locked before the corresponding implementation phase. The technical safety defaults are already in this plan and are not open options:

| Decision | Options | Decided default | Consequence of leaving it open |
|---|---|---|---|
| Profile scope | user-global; or project-scoped | **user-global**, explicitly stated in the UI; do not modify EverOS | `/get` reads a single owner-keyed row today and cannot prove project isolation (see §9.3) |
| `bounded` success criterion | flush confirmed successful; or LanceDB visible | **flush confirmed successful**, result still labeled `indexing eventual`; do not promise immediate searchability | wait/overlay semantics drift |
| `unknown` without receipt | `manual_required`; or at-least-once replay | **`manual_required`**, no auto-replay | Cannot safely choose between duplicate memory and a permanent stall |
| Attachment recovery SLA | pin-before-accept and include in backup; or metadata only | **pin-before-accept**, attachments directory folded into the consistency snapshot | accepted captures may be unreplayable |
| Final flush when outbox has not drained | wait for the target session to drain; or fence subsequent adds | **Fence first, then short-wait drain; on timeout, persist `due` and block old-generation adds under the fence** | Post-close adds may miss flush |
| New add during in-flight flush | provider generation; or Avibe session fence | **Avibe session fence**, because EverOS does not currently accept generation IDs | watermark cannot be enforced; settlement may cross generations |
| Agentic search explicit enable | enabled by default; or explicit opt-in | **Explicit opt-in**, subject to §9.2.2 budgets, see §9.2.1/§9.2.4 | Ordinary call paths are polluted by an expensive capability |
| Agentic capability gating | fallback on missing capabilities; or `capability-unavailable` | **`capability-unavailable`**, no fallback, see §9.2.4 | Silent regression to `hybrid` hides capability gaps |
| Agentic `timeout` / `max_model_calls` / `max_results` / `cost_budget_tokens` missing or zero | forward to EverOS default; or fail closed | **fail closed at the adapter**, caller must declare each of the four fields explicitly, see §9.2.2 | Forwarding to provider defaults risks unexpected costly LLM work without an explicit budget; treating any one field as optional silently re-opens the same risk |
| Embedding rotation entry point | CLI command; or Web UI | **Web UI, collapsed + second confirmation**, see §8.4 | CLI entry spreads misuse; UI entry exposed on the main path causes accidental triggers |
| First-version bounded-wait user action | Yes; or internal only | **Internal only** (§5.2) | Users see no controllable wait in the UI |
| `manual_required` resolution operator path | none; or audited manual operation | **Audited manual operation** through `operate` (§5.5) | Without it, `manual_required` blocks the session indefinitely or forces a destructive `clear` |

Out of scope for this plan:

- Knowledge-base Markdown style customization (schema/style split, user-level / project-level config): not delivered by this plan; open a separate plan if needed.
- A CLI-shaped rotation command: consistent with §8.4, no CLI surface; equivalent operations reuse the internal `operate` interface exposed by the Web UI flow.

Non-blocking product / schedule choices; the recommended defaults are safe to proceed with:

1. Idle flush defaults to 5 minutes, max-age defaults to 30 minutes;
2. `/new`, archive, and explicit session close all trigger final flush;
3. The first version does not provide a "searchable immediately" bounded-wait user action; the internal-interface semantics stay;
4. The first version continues to capture user messages only; no assistant / tool / agent memory;
5. Adopted: configure reranker for `agentic` search, with `agentic` explicit opt-in and budget-constrained; this version does not enable assistant / tool / agent capture and does not modify EverOS;
6. Embedding semantic rotation allows downtime initially; the entry is via Web UI (see §8.4), and is not opened until journal/fence ship;
7. Provider-payload diagnostics continue under today's default-enabled behavior; privacy review is a separate track;
8. EverOS 1.2.3 artifact ships on an independent release track;
9. Cascade permanent data-quality failure is operator diagnostics only and does not map to a global degraded state.

## 15. Key Source References

### Avibe

- `core/memory/everos.py`: EverOS UDS HTTP adapter, search, and error mapping;
- `core/memory/sidecar.py`: sidecar route/shape guard;
- `core/memory/process.py`: managed child, UDS, and lifecycle;
- `core/memory/worker.py`: current per-message flush and queue drain;
- `core/memory/store.py`: capture queue, flush observation, boot recovery;
- `core/memory/module.py`: current Memory interface and status precedence;
- `core/memory/runtime.py`: controller-owned lifecycle, embedding guard, and status payload;
- `core/memory/everos_insight/`: provider-call recorder and processing log;
- `docs/plans/everos-1.2.1-upgrade.md`: completed history of the EverOS 1.2.1 / `project_id` upgrade;
- `docs/plans/memory-architecture-deepening.md`: completed history of the Memory deep-module refactor.

### EverOS (pinned revision `48fc908`)

- `CONTEXT.md`: integration domain glossary;
- `EVEROS_INTEGRATION_zh.md`: integration contract and operations guidance;
- `src/everos/service/memorize.py`: add/flush scheduling;
- `src/everos/service/_boundary.py`: buffer, memcell, and boundary order;
- `src/everos/service/_session_lock.py`: same-session concurrency semantics;
- `src/everos/entrypoints/api/routes/health.py`: health / liveness / readiness;
- `src/everos/entrypoints/api/routes/metrics.py`: Prometheus endpoint;
- `src/everos/core/middleware/prometheus.py`: current HTTP metrics;
- `src/everos/entrypoints/cli/commands/cascade.py`: controlled projection rebuild;
- `src/everos/infra/persistence/sqlite/tables/unprocessed_buffer.py`: unprocessed messages;
- `src/everos/infra/persistence/sqlite/tables/memcell.py`: raw dialog archive after boundary;
- `src/everos/infra/persistence/sqlite/tables/cluster.py`: embedding-derived cluster centroid;
- `src/everos/memory/search/dto.py`: four-mode search and request semantics.
