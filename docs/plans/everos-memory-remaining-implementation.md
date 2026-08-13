# EverOS Memory Remaining Implementation Spec

Status: implementation contract

This specification implements the remaining PR defined by
[`everos-memory-adjustment.md`](./everos-memory-adjustment.md). The umbrella plan is the product
decision record; this file freezes the concrete Avibe schema, state transitions, CAS rules, source
contracts, file ownership, and validation for the implementation branch.

## 1. Module boundaries

The public Memory seam remains provider-neutral:

- `MemoryModule.capture()` validates and durably admits one user capture.
- `MemoryModule.recall()` executes exactly one `RecallPolicy` decision and at most one provider search.
- `SessionFlushCoordinator` exclusively owns session generations, fences, explicit flushes, and flush
  recovery. `MemoryWorker` only claims and delivers add rows through the coordinator.
- `AttachmentPinStore` owns private durable attachment bundles and crash-safe release.
- `MemoryMaintenance` owns the durable clear-intent marker and four idempotent deletion primitives.
  Runtime lifecycle code only establishes the maintenance fence, stops/starts owned processes, and
  invokes this seam.
- Processing Record directly projects public EverOS `/health`, existing call-log/processing provenance,
  and confirmed Avibe settlement facts. The outbox and lifecycle do not compose product status.

No platform adapter, agent backend, release manifest, private EverOS database, or EverOS source is in
scope.

## 2. Canonical session identity

`ProviderSessionRef(principal_id, epoch, project_ref, session_id)` is the only coordination key. Its
deterministic JSON serialization is stored in every session-scoped SQLite row and is the key passed
between Avibe modules. Store methods accept `ProviderSessionRef` or a typed lease, never a bare session
ID or `(session_id, project_ref)` pair.

The EverOS 1.2.3 public request caps `session_id` at 128 bytes. The adapter therefore accepts the full
typed reference and performs the only wire projection: `session_id=ref.session_id` and
`project_id=ref.project_ref`. The bounded `ref.session_id` is already derived from the principal,
project, original session anchor, and epoch. `/add` and `/flush` must receive the same typed reference;
callers cannot construct either wire field independently.

## 3. Durable schema

This is a clean-schema change. Migration, inspection, or preservation of pre-implementation Memory
databases is explicitly out of scope.

### 3.1 Capture outbox

`memory_capture_queue` retains capture delivery only. Remove every `flush_*` column. Add:

- `generation INTEGER NOT NULL CHECK (generation >= 1)`;
- `lease_token INTEGER NOT NULL DEFAULT 0 CHECK (lease_token >= 0)`;
- `add_status TEXT NULL CHECK (add_status IN ('accumulated', 'extracted'))`;
- `attachment_bundle_id TEXT NULL` referencing the attachment bundle table.

The queue continues to retain the canonical serialized reference and the closed states `pending`,
`processing`, `delivered`, `dead`, and `manual_required`. Claim increments `lease_token`; every settle
CAS includes digest, epoch, lease owner, and lease token. `manual_required` retains payload and pinned
attachments. Confirmed delivered/dead outcomes scrub text and mark an attachment bundle for release.

### 3.2 Attachment bundles

`memory_attachment_bundle` contains:

```text
bundle_id TEXT PRIMARY KEY                 # random 128-bit hex
relative_path TEXT UNIQUE NOT NULL
state TEXT NOT NULL                        # pinned | releasing
file_count INTEGER NOT NULL
total_bytes INTEGER NOT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

Versioned attachment JSON stores only `kind`, display `name`, `ext`, bundle-relative `storage_key`,
`size_bytes`, and SHA-256. Absolute paths and source URIs are never persisted in the queue payload.

### 3.3 Flush authority and evidence

`memory_session_flush_state` contains one row per serialized provider reference:

```text
provider_session_ref TEXT PRIMARY KEY
epoch INTEGER NOT NULL
open_generation INTEGER NOT NULL
target_generation INTEGER NULL
state TEXT NOT NULL                       # idle | due | in_flight | manual_required
first_unflushed_at TEXT NULL
last_add_ack_at TEXT NULL
confirmed_add_watermark_ms INTEGER NULL
unflushed_count INTEGER NOT NULL
due_at TEXT NULL
next_attempt_at TEXT NULL
retry_count INTEGER NOT NULL
operation_epoch INTEGER NOT NULL
fence_token TEXT NULL
submission_started_at TEXT NULL
updated_at TEXT NOT NULL
```

`memory_flush_settlements` is append-only and records:

```text
settlement_id INTEGER PRIMARY KEY AUTOINCREMENT
provider_session_ref TEXT NOT NULL
epoch INTEGER NOT NULL
generation INTEGER NOT NULL
operation_kind TEXT NOT NULL               # add | flush
operation_token TEXT NOT NULL
observation TEXT NOT NULL                  # settled | rejected | manual_required
request_id TEXT NULL
confirmed_watermark_ms INTEGER NULL
observed_at TEXT NOT NULL
error_code TEXT NULL
UNIQUE(provider_session_ref, epoch, generation, operation_kind, operation_token)
```

Terminal settlement rows cannot be updated or deleted. Public anomaly projections omit the canonical
reference and operation/fence token.

## 4. Flush state machine and CAS

Defaults are five minutes idle, thirty minutes maximum age, 100 acknowledged messages, three proven
pre-submission attempts, and bounded exponential retry of 1, 2, then 4 seconds.

1. Enqueue ensures a session state row and assigns its current `open_generation`. If a fence exists,
   the capture is still accepted into the already advanced next generation.
2. Normal worker claims exclude sessions in `due`, `in_flight`, or `manual_required`. Delivery and
   fencing are serialized by the coordinator per exact provider reference; unrelated references may
   progress under the existing global provider concurrency bound.
3. An `accumulated` add ack atomically settles the exact row lease, advances the watermark and count,
   sets `first_unflushed_at` only for the first ack in the generation, and computes
   `due_at=min(last_add_ack+idle, first_unflushed+max_age)`. The message bound makes it immediately due.
4. An `extracted` add ack writes an `add/settled` record for the exact generation and lease token,
   advances the generation, reassigns still-pending rows to the new generation, and releases an exact
   matching fence without calling `/flush`.
5. A due or explicit final flush first CAS-acquires a fence. It freezes `target_generation`, advances
   `open_generation`, increments `operation_epoch`, creates a fresh `fence_token`, and sets `due`.
6. While holding the per-session fence, the coordinator drains target-generation rows. A worker row
   claimed immediately before the fence is returned to pending and then drained by the fence owner.
7. Immediately before the HTTP call, CAS on exact ref/generation/token changes `due` to `in_flight`
   and persists `submission_started_at`. Only then may the adapter call `/flush`.
8. Settlement CAS includes exact ref, epoch, target generation, fence token, state, and submission
   marker. A stale result changes nothing. Confirmed success or rejection writes immutable evidence and
   releases the next generation. Ambiguous outcomes write `manual_required`, retain the fence, and
   never replay.
9. A proven pre-submission failure leaves `submission_started_at` null and schedules bounded retry.
   Exhaustion becomes `manual_required`. On boot, a durable `due` row with no submission marker is
   retryable; `in_flight` or any marker-bearing interrupted operation becomes `manual_required`.

Malformed/unsupported 2xx, missing receipt/request ID, timeout, disconnect, and response loss are
ambiguous. A received non-2xx response is a confirmed rejection. Provider health never rewrites one
outcome into another. Runtime shutdown cancels tasks after a short bound and persists unsubmitted work
as due; it never starts a final provider call.

Central lifecycle hooks may request `final_flush(ref, deadline)` only when they already hold a trusted
canonical reference. The initial hooks are `/new`, Workbench archive when the full reference is
available, and the central explicit session-close seam. No IM adapter receives flush logic.

## 5. Durable attachments

Attachment storage is `<effective_home>/memory/attachments/{staging,bundles}`. Directories are mode
0700 and files mode 0600. A bundle contains at most eight files, each at most 25 MiB, with a 100 MiB
capture total.

Pinning uses a no-follow file descriptor, verifies root confinement, owner, parent/file type and mode,
streams copy plus SHA-256 under the size bound, verifies source `fstat` did not change, fsyncs files and
directories, and atomically renames staging to the bundle path. Unsafe/missing/mutated input returns
`memory_invalid_input`; size excess returns `memory_input_too_large`; insufficient space returns
`memory_low_disk_space`; copy/fsync/rename failure returns `memory_store_unavailable`. Capture cannot
return `accepted` until pinning and the enqueue transaction both commit.

Duplicate/rejected admission deletes the unreferenced new bundle. Confirmed add delivery or dead
rejection marks the bundle `releasing` in the same settle transaction, then idempotently removes its
files and finalizes the bundle row. Boot reconciliation completes `releasing` bundles and removes only
unreferenced staging/orphan bundles inside the private root. Pending and `manual_required` bundles are
never removed.

## 6. Durable clear intent

Clear Memory Data uses `<effective_home>/state/memory/clear-intent.json` as its single
source of truth. New markers contain a UUID4 operation id; migrated markers preserve the
legacy operation id for log correlation. The marker also contains operator reference,
pre/target epochs, `deleting|failed` state, error code, and creation/update timestamps. It
is atomically written through a same-directory temporary file, file fsync, replace, and
parent-directory fsync.

The operation fences claims and runs exactly four idempotent deletion primitives: queue
reset, provider-root recreation, call-log clear, and attachment clear. Completion clears
the in-progress flag and removes the marker. An interrupted or failed marker is retried
automatically on the next reconcile; corrupt or unreadable marker state fails closed for
Memory projection while service startup continues. An explicit Clear request can replace
a corrupt marker. There is no backup/restore journal or user-facing resume/abort API.

## 7. Recall policy

Each request carries exactly one closed `RecallPolicy`:

```text
mode: auto | keyword | vector | hybrid | agentic     # default hybrid
max_results: integer                                 # default 8, range 1..20
include_profile: boolean                             # default true
include_current_session: boolean                     # default false
timeout_seconds: positive number | null
max_model_calls: positive integer | null
cost_budget_tokens: positive integer | null
```

Agentic limits are `timeout_seconds <= 30`, `max_model_calls <= 4`, `max_results <= 20`, and
`cost_budget_tokens <= 32000`; all four must be explicitly non-zero. Non-agentic policies reject
agentic-only budget fields. Unknown keys, `declarations`, arbitrary filters, and caller-supplied session
IDs are rejected.

`keyword` requires no embedding. Explicit `vector`/`hybrid` require a latest trustworthy health
observation declaring embedding capability. `auto` chooses hybrid only when embedding is explicitly
available, otherwise keyword, and never agentic. Missing/unknown required capability returns
`memory_capability_unavailable` with zero provider calls and no fallback.

EverOS 1.2.3 neither accepts nor declares enforcement for model-call or token ceilings. Its adapter
therefore declares `agentic_budget_enforced=false`: even a valid complete agentic policy returns
`memory_capability_unavailable` and performs zero searches. This changes only when a future public
EverOS contract both declares and enforces those ceilings; a local timeout is not sufficient.

`include_current_session` accepts only the trusted caller-session header, resolves it through the
current principal/project/epoch to a canonical reference, and emits the sole provider session filter.
Missing or mismatched trusted context fails closed. Profile remains user-global and owner-keyed.
Results publish requested/effective mode, source, overlay use, `watermark_ms` (null when unavailable),
and freshness (`unknown` when the provider gives no marker). One recall performs at most one `/search`;
a provider rejection never triggers another mode.

## 8. Processing Record contract

Keep the existing route family but remove `MemoryModule.status()` and the global
`ready|syncing|degraded|down` projection. `/api/memory/status` becomes a bounded direct projection:

```json
{
  "status": "ok",
  "source": {
    "status": "available",
    "observed_at": "2026-08-08T12:00:00Z",
    "reason": null
  },
  "health": {
    "status": "ok",
    "version": "1.2.3",
    "capabilities": {},
    "disabled_features": [],
    "cascade": {},
    "recorder": {}
  }
}
```

The provider port performs one typed `/health` read. Only allowlisted fields are returned; reason lists
are closed/scrubbed, item-limited, and length-limited. A successful read is `available` with the local
observation time. A later read failure returns the last successful snapshot as `stale` with its original
time; no prior snapshot is `unavailable` with `health=null`. Cascade health is a source fact, not a
global readiness claim. Page reads do not invoke processing probes or provider-root scans.

Processing Record has four independently degradable areas: runtime summary, source availability,
recent confirmed pipeline provenance, and anomalies/recovery plus bounded diagnostic detail. Existing
call-log/insight pagination, authorization, redaction, response-size, and detail limits remain. Each
source section carries `observed_at` or explicit unknown/unavailable. Timeline steps are emitted only
from provider provenance or immutable add/flush settlement evidence, never inferred from queue state.

Anomalies expose only kind, state, operation, time, closed error, attempts, generation, and bounded
request ID. They include confirmed provider rejection, `manual_required`, boot recovery, clear
recovery, and recorder degradation. They never expose canonical refs, fence/lease tokens, payloads,
absolute paths, attachment metadata/hashes, vectors, or raw exceptions. `manual_required` is read-only.

The UI merges status and processing-log tabs into one Processing Record view with explicit Refresh,
runtime/capabilities, source availability, anomalies/recovery, recent timeline, and existing detail.
It does not poll the composite status every four seconds. Disabled Memory still renders retained local
recovery/anomaly evidence. Clear recovery is marker-owned and has no user-facing resume/abort
commands; `manual_required` has no command. Embedding configuration lock reads a separate cheap local
`data_exists` maintenance fact rather than health or a deep provider-root scan. All copy uses i18n.

## 9. File scope

Expected backend changes are confined to `core/memory/` (schema, types, store, worker, coordinator,
attachments, snapshot, clear journal, EverOS port, runtime/module, and insight projection), existing
internal Memory routes/client, and centralized session lifecycle handlers that can provide the full
reference. Frontend changes are confined to the existing Memory settings/Processing Record components,
API types, and English/Chinese i18n. Update `docs/MEMORY.md`.

Do not update `vibe/memory_runtime_manifest.json` without real published immutable artifacts. Do not
add an EverOS API, private DB reader, metrics dependency, manual-resolution operation, data rebuild,
multi-run search, platform aliasing, or assistant/tool/agent capture.

## 10. Validation matrix

- Store/coordinator: schema, generation assignment, idle/max-age/message triggers, fence-first capture
  race, same-ref serialization, unrelated-ref progress, natural extraction without flush, exact stale
  CAS, retry cap, malformed/timeout/disconnect terminal behavior, boot before/after submission, and
  shutdown with no provider write.
- Adapter: typed ref body, strict add/flush receipts, pre-submission versus ambiguous transport,
  complete/scrubbed single-call health, four non-agentic method payloads, keyword without embedding,
  and agentic capability fail-closed.
- Attachments: confinement/symlink/special-file/owner/mode checks; byte limits; mutation detection;
  fault injection at copy/fsync/rename/DB boundaries; original deletion after accepted pin; duplicate
  cleanup; confirmed release; manual/stale retention; boot release/orphan convergence; no leakage.
- Clear intent: crash after marker creation, each deletion primitive, queue-fence clearing, and marker
  removal; automatic boot retry; one marker-owned operation; idempotent four-surface deletion; corrupt
  marker fail-closed projection; operation fences; legacy journal migration.
- Processing Record: available/stale/unavailable health; Cascade unhealthy as a valid observation;
  independently locked/corrupt/schema-incompatible sources; confirmed-only timeline; anomalies and
  clear recovery; authorization, pagination, bounds, and redaction; no active probes/root scans.
- Recall: validation for every mode/budget, auto selection limited to keyword/hybrid, zero-call missing
  capability, exactly one provider search, no rejection fallback, trusted-only session overlay,
  user-global profile, and explicit source/watermark/freshness serialization.
- UI: merged tab, source-independent failures, disabled recovery visibility, read-only
  `manual_required`, clear recovery actions, separate maintenance data fact, responsive rendering, and
  `npm run build`.
- Run focused Memory/UI tests, changed-Python `ruff check`, broader relevant pytest coverage, and the UI
  production build before push.
