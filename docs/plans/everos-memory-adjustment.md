# EverOS Memory Integration Adjustment

> Status: implemented / PR review
>
> Date: 2026-08-08
>
> Target branch: Avibe `dev`
>
> This document is the source of truth for this implementation and review. It records the
> owner-approved scope, the two capabilities already merged into `dev`, and the one remaining
> implementation PR. This umbrella plan is tracked with that implementation.
>
> The implementation must start from the latest `origin/dev`, not from any deleted staging branch
> or abandoned worktree. The owner will review the resulting Processing Record UI after it is built;
> a separate mockup approval round is not required.

## 1. Delivery status and objective

### 1.1 Already implemented

#### PR #1224 — EverOS runtime 1.2.3

Merged into `dev`:

- merge commit: `b7350bdcb834edc1b7ea1bfabb492cc08461d412`;
- EverOS runtime pin and lock/provenance updates for 1.2.3;
- release/manifest guard that refuses to claim availability without real immutable assets.

The manifest may remain `release_state: unavailable` until the release process publishes and verifies
real immutable artifacts. Do not fabricate assets, URLs, or checksums.

#### PR #1230 — durable Memory foundation

Merged into `dev`:

- PR: <https://github.com/avibe-bot/avibe/pull/1230>;
- merge commit: `a104df57ef7debf7f2782e360e475282d9e136d8`;
- clean new Memory schema;
- canonical `ProviderSessionRef` identity:
  `(principal_id, epoch, project_ref, session_id)`;
- deterministic identity serialization and validation;
- durable capture enqueue, idempotency, claim, success settlement, and boot recovery;
- strict structural validation of successful `/add` receipts;
- malformed, timeout, disconnect, and otherwise ambiguous `/add` outcomes become terminal
  `manual_required` and are never silently replayed;
- conservative transport classification for `ReadError`, `RemoteProtocolError`, `WriteError`, and
  `CloseError`;
- deterministic non-retryable provider rejection remains terminal even when a health probe fails;
- failure-log and status-foundation visibility for terminal failures.

PR #1230 deliberately does **not** contain flush coordination, generation routing, natural-boundary
projection, attachment pinning, clear journaling, Processing Record changes, or search-policy
expansion. Those belong to the one remaining implementation PR below.

### 1.2 Objective of the remaining PR

Deliver the remaining approved Memory behavior as one coherent PR into `dev`:

1. authoritative session flush coordination;
2. durable attachment handling and crash-safe clear;
3. Processing Record backend and UI;
4. single-run search policy and capability gating;
5. focused contract/scenario tests and UI build validation;
6. release-manifest completion only when the real immutable runtime artifacts are available.

This is one delivery unit, not a sequence of artificial PR1–PR9 stages. The Agent may split the work
internally, but the final PR must have one coherent contract and one review loop.

## 2. Non-negotiable product decisions

Memory is a new, unused feature. There are no existing users or production Memory data to preserve.
Therefore:

- do not inspect, detect, or infer an old schema;
- do not write migrations, compatibility conversion, legacy import, or `migrated_legacy` rows;
- do not preserve or rebuild historical user data;
- start from the clean schema already merged by PR #1230;
- do not implement a broad user-data rebuild workflow or rebuild button in this delivery;
  the later, narrowly scoped embedding-identity recovery in issue #1314 is defined by
  `docs/plans/memory-embedding-rebuild-recovery.md`;
- do not implement an audited manual-resolution `operate` API, decision rows, fence-release UI, or
  manual-resolution workflow;
- keep `manual_required` as a durable, visible, terminal safety state with no automatic replay;
- keep search single-run only; no multi-declaration or fan-out request body;
- defer cross-platform IM alias pairing;
- defer a caller-facing bounded-freshness action/API in the first version; internal bounded waits may
  still be used by coordination logic;
- do not add assistant/tool/agent capture;
- do not modify EverOS source code;
- do not add a private EverOS status protocol, business-metrics API, or deep provider-root scan;
- do not make `/metrics` a core Processing Record dependency;
- do not fabricate release assets or checksums.

The existing `restart` and `clear` lifecycle operations may remain where already supported. `clear`
is destructive and must not be treated as a resolution of `manual_required`; it must use the
crash-safe journal described in §7.

## 3. Architecture and boundaries

Continue using the Avibe-managed EverOS child process over private UDS HTTP. Do not import EverOS
in-process and do not expose the UDS as a TCP/public service.

```text
Avibe controller
  -> Memory runtime/lifecycle
  -> durable capture outbox + session flush coordinator
  -> EverOS UDS HTTP adapter
  -> managed EverOS child
```

The public Avibe Memory seam remains provider-neutral:

- `capture` accepts a capture and returns only durable-local acceptance/duplicate semantics;
- `recall` accepts an Avibe-owned single-run search policy and returns Avibe-owned results;
- existing maintenance operations retain their current narrow behavior, but no new manual-resolution
  operation is introduced.

The outbox and lifecycle remain reliability mechanisms, not a second product status system.

### 3.1 Canonical identity contract

The exact serialized `ProviderSessionRef` from PR #1230 is the sole provider-session identity:

```text
(principal_id, epoch, project_ref, session_id)
```

Every session-scoped capture, `/add`, generation, fence, `/flush`, settlement, retry, recovery,
attachment reference, and processing record must use this identity. Never key coordination by bare
`session_id`, `(session_id, project_ref)`, or `app`.

A different logical session may progress concurrently. The same canonical provider session must be
serialized for `/add` and `/flush`; the existing global provider concurrency cap still applies.

### 3.2 Provider-write outcome contract

For every provider write:

- confirmed success requires a structurally complete, supported response and valid receipt/request ID;
- a failure proven before submission may use bounded retry when explicitly retryable;
- timeout, disconnect, malformed/unsupported 2xx, or any outcome that may follow submission is
  ambiguous and becomes terminal `manual_required` with evidence retained;
- ambiguous writes are never silently auto-replayed;
- deterministic non-retryable provider rejection is terminal and must not be rewritten as a temporary
  outage merely because a health probe is unhealthy.

No implementation may claim exactly-once provider extraction without an EverOS receipt/reconciliation
capability that the current public interface does not provide.

## 4. Remaining PR: implementation contract

The implementation worktree tracks one concrete implementation spec under `docs/plans/`. It cites this
document and freezes the concrete schema, state transitions, operation-token CAS rules, file scope,
source contract for the Processing Record, and test matrix.

### 4.1 Authoritative session flush coordinator

Remove flush-after-every-successful-add. Implement these triggers only:

- idle timeout (initial default recommendation: five minutes);
- maximum unflushed age and/or message bound to prevent starvation (initial max-age recommendation:
  thirty minutes);
- internal explicit final flush for `/new`, archive, and centralized explicit session close hooks,
  only where the canonical identity is available;
- durable boot recovery of `due` and interrupted flush state.

The coordinator owns the smallest durable state needed for recovery and settlement:

- canonical provider session reference;
- current/open generation;
- `first_unflushed_at` that is not reset by every continuous message;
- latest confirmed add watermark and `last_add_ack_at`;
- `due_at` and bounded retry `next_attempt_at`;
- flush state (`idle`, `due`, `in_flight`, and terminal `manual_required`, or an equivalent minimal
  set that makes recovery unambiguous);
- monotonically increasing `fence_epoch` / operation token;
- exact settlement evidence for the acquired generation.

#### Fence-first ordering

Final flush is ordered strictly:

1. acquire and persist the session fence and operation token;
2. while holding the fence, drain or short-wait for rows belonging to the fenced generation;
3. call EverOS `/flush` with the exact canonical provider session reference;
4. settle by compare-and-set against the exact session, generation, and fence token;
5. release next-generation delivery only after safe settlement.

Checking that the queue is drained before acquiring the fence is forbidden. A capture arriving during
an in-flight flush is accepted and persisted into the next generation. A same-session worker may not
claim/send `/add` while that flush fence is in flight. A different canonical session may continue.
Shutdown persists `due`/incomplete state rather than blocking indefinitely on an LLM flush.

#### Add acknowledgements and natural boundaries

- `status=accumulated` advances the add watermark and updates idle due time while preserving the
  generation's original `first_unflushed_at`; continuous traffic must not starve max-age.
- `status=extracted` is an authoritative natural-boundary settlement for the exact current generation;
  it advances the watermark and opens/recomputes the next generation from remaining rows. Do not issue
  a redundant explicit `/flush` for a naturally settled generation.
- Settlement evidence records operation kind (`add` natural boundary or explicit `flush`), exact
  provider session/generation/token, observation (`settled`, `rejected`, or `manual_required`), receipt
  ID when valid, confirmed watermark, observed time, and error code where applicable.
- Stale settlement cannot clear or overwrite a newer fence.

#### Flush retry and recovery

- Proven pre-submission, explicitly retryable failures use bounded exponential backoff and a cap.
- Confirmed non-retryable provider rejection settles terminally.
- Exhausted retry and repeated uncertain outcomes become terminal `manual_required` and remain visible.
- Structurally invalid 2xx, timeout, disconnect, and post-submission-capable transport failure become
  `manual_required`, retain the fence/evidence, and are not retried automatically.
- An `in_flight` flush found after process death is `manual_required` unless durable evidence proves it
  was never submitted.

Do not grow `MemoryWorker` or `MemoryStore` into an unbounded state machine. Prefer a cohesive
`SessionFlushCoordinator` seam with narrow transactional/CAS store methods. Keep lifecycle wiring
limited to centralized hooks that can pass canonical identity; do not fan out into speculative changes
across every platform adapter.

### 4.2 Durable attachments

An attachment is recoverable only after its bytes are copied or pinned into Avibe-owned private durable
storage before the capture is reported as recoverably accepted.

Required behavior:

- enforce attachment-root confinement and symlink/privacy checks;
- keep the durable attachment reference in the capture row;
- clean payload/attachment data only after confirmed provider settlement according to PR #1230 rules;
- include attachment bytes and metadata in the runtime backup/restore consistency scope;
- never put attachment bytes or secrets into logs, Processing Record responses, or provider diagnostics;
- define bounded size/retention behavior and explicit failure when bytes cannot be pinned.

### 4.3 Crash-safe clear journal

`clear` is destructive and is not a manual resolution mechanism. Because the live queue, any replay
state, and provider-owned data may live in separate durable surfaces, clear must use a two-phase journal:

1. under the maintenance fence, write an idempotent open journal containing operator identity, start
   time, pre-clear digests, and restorable snapshots/paths before destructive work;
2. delete the explicitly owned local Memory surfaces and record each substep completion;
3. mark completion only after all substeps succeed, retaining an immutable completion/audit record;
4. after a crash, surface `recovery_needed` and require an explicit resume or abort path; never silently
   replay quarantined rows, silently delete more data, or treat `manual_required` as resolved.

If rollback/abort is supported, restore every surface covered by the journal, not only one SQLite file.
Open journal state must be included or explicitly block a backup candidate so restore cannot combine
incompatible queue/provider/journal states.

### 4.4 Processing Record backend

Fold the useful Memory runtime summary into the existing Processing Record page. Do not create a
separate status model or a composite global `ready/syncing/degraded` claim.

The backend contract has four read-only areas:

1. **EverOS runtime summary** — latest successfully read public `GET /health`, including version,
   declared capabilities, Cascade fields, recorder fields, and `observed_at` when present;
2. **Recent pipeline** — existing Avibe processing/call-log provenance for directly confirmed
   `capture -> add/flush -> memcell -> episode -> OME -> profile/skill -> indexing` steps;
3. **Anomalies and recovery** — confirmed add/flush failures, boot recovery, clear-journal recovery,
   recorder degradation, and terminal `manual_required` evidence;
4. **Diagnostic detail** — existing bounded, scrubbed, body-redacted processing/call detail with current
   permission, pagination, and response-size limits.

Source rules:

- every summary carries `observed_at` or is marked unknown/unavailable;
- missing, stale, locked, corrupt, or schema-incompatible sources degrade only that local source;
- `/health` success never means the entire Memory pipeline is ready;
- ordinary page reads do not invoke active processing probes or deep provider-root scans;
- `/metrics` is optional and not a core dependency;
- no new EverOS business-metrics API;
- no new private EverOS SQLite reader or private EverOS status protocol;
- do not implement profile/data rebuild or an index rebuild workflow as part of this page;
- `manual_required` is visible read-only; no resolution button, decision form, or fence-release action.

Reuse existing Avibe processing/call-log interfaces and UI data shapes where possible. If an existing
adapter cannot read a source safely, return `unknown`/`unavailable` rather than inventing a complete
status. Do not expand a pinned-runtime adapter into a new global state machine.

### 4.5 Processing Record UI

Implement the UI directly in the same PR; no separate mockup gate. The owner will inspect the actual
page and request follow-up visual changes if needed.

The page should clearly separate:

- EverOS runtime/capability summary and observation time;
- source availability;
- recent processing timeline/list and detail;
- anomalies, recovery notices, and read-only `manual_required` records.

Use existing UI primitives, existing layout conventions, and frontend i18n files. Never hardcode
user-facing strings in React components. Keep bodies, secrets, raw attachment bytes, vectors, and
unrelated high-cardinality IDs out of the rendered page. Validate with `cd ui && npm run build`.

### 4.6 Single-run search policy

Ship one Avibe-owned `RecallPolicy` per request with these modes:

- `keyword` — precise names, IDs, error codes, and terms;
- `vector` — semantic similarity;
- `hybrid` — general default;
- `agentic` — explicit opt-in only.

No `declarations` list, duplicate-key trick, multi-run execution, or fan-out ceiling is needed in this
first delivery.

Policy requirements:

- `auto` may choose only among non-agentic modes and never implicitly escalates to `agentic`;
- `agentic` requires explicitly declared non-zero `timeout_seconds`, `max_model_calls`,
  `max_results`, and `cost_budget_tokens`; missing capability or budget fails closed with an explicit
  `capability_unavailable`/validation result and never silently falls back to `hybrid`;
- `keyword` remains usable when embedding is unavailable;
- profile reads follow the decided user-global owner-keyed path;
- session overlays accept only the trusted current canonical session and reject arbitrary caller
  `session_id` filters;
- freshness/source/watermark markers remain explicit, but caller-facing bounded-freshness controls are
  deferred from the first UI/API surface;
- cross-platform IM alias pairing is deferred.

## 5. Explicit non-goals and deferred work

The one remaining PR must not absorb the following:

- old schema inspection, migration, compatibility, legacy preservation, or historical-data rebuild;
- broad user-data/index/profile rebuild workflows or generic rebuild actions; issue #1314 adds only
  the confirmed embedding-identity recovery defined in `memory-embedding-rebuild-recovery.md`;
- audited manual operation, manual decision rows, or manual-resolution UI;
- multi-run/fan-out search;
- cross-platform IM alias pairing;
- private EverOS SQLite/status protocol;
- deep provider-root scans on ordinary page reads;
- new EverOS business-metrics APIs;
- assistant/tool/agent capture;
- exactly-once claims that require unavailable EverOS receipts/reconciliation;
- release artifacts or checksums that have not been published and verified.

Automatic or generic semantic embedding rotation remains future/gated work. Issue #1314 is a narrower
follow-up: an explicit confirmed settings change persists one recovery intent and runs the already
supported pinned-child rebuild without adding background jobs, automatic repair, or a general operations
framework. Its contract lives in `memory-embedding-rebuild-recovery.md`.

## 6. File and ownership boundaries

Expected implementation files include:

- `core/memory/schema.sql`, `types.py`, `store.py`, `worker.py`, and one cohesive coordinator module;
- `core/memory/module.py` / `runtime.py` only for centralized final-flush, clear-journal, and lifecycle
  interfaces;
- attachment storage/backup modules where an existing seam exists;
- Processing Record backend/read contracts and existing Memory log handlers;
- `ui/` Processing Record page/components, styles, and i18n files;
- `docs/MEMORY.md` and one tracked implementation spec under `docs/plans/`;
- focused Memory and UI tests.

Do not touch platform adapters, agent backends, generic controller routing, or release files unless a
concrete end-to-end contract requires it and the implementation spec records the reason. Do not modify
or commit this umbrella plan from the implementation worktree.

## 7. Required validation and review

Before opening the PR:

- run focused store/coordinator/worker/module/runtime tests;
- run broader Memory tests;
- run attachment and clear-journal crash/fault tests;
- run Processing Record source-degradation and redaction tests;
- run single-run search policy and mode-capability tests;
- run `ruff check` on every changed Python file;
- run `cd ui && npm run build` when UI changes;
- run relevant scenario/contract tests and name scenario IDs in the PR description when a catalog
  exists;
- perform an independent quality review of the complete diff for correctness, simplicity, scope, and
  test quality before merge.

Delivery rules:

- branch from latest `origin/dev` and open one real non-draft PR targeting `dev`;
- automatic Codex review only; do not manually comment `@codex review`;
- poll the automatic review and CI state at one-minute intervals; do not create a managed background
  watch for this delivery;
- if the same architectural review theme repeats twice, stop and revisit the contract instead of adding
  another patch layer;
- merge only when CI is green, the latest-head Codex review is clean, zero unresolved review threads
  remain, `mergeStateStatus == CLEAN`, and the implementation Session is quiescent. The orchestrator
  may merge directly once those mechanical gates pass.

## 8. End-state acceptance checklist

### Flush and durability

- Continuous successful adds do not trigger one flush each.
- Idle, max-age/message-bound, explicit final flush, and boot recovery work durably.
- Final flush acquires the fence before checking drain and holds it through `/flush`.
- Captures during a flush persist in the next generation; same-session delivery waits; unrelated
  sessions continue.
- Settlement is CAS-protected by exact canonical session, generation, and fence token.
- Natural `status=extracted` settles the exact generation without redundant `/flush`.
- `first_unflushed_at` does not move on every message; max-age cannot starve.
- Ambiguous flush outcomes become terminal `manual_required`, retain evidence, and never auto-replay.
- Shutdown does not block indefinitely on an LLM flush.

### Attachments and clear

- An accepted recoverable attachment is pinned in Avibe-owned private durable storage.
- Attachment paths are confined, privacy-safe, backed up, and never logged/rendered as raw bytes.
- Clear writes its journal before destructive work, records substeps, and retains completion evidence.
- Crash recovery cannot silently delete data, replay cleared data, or convert `manual_required` into a
  successful resolution.

### Processing Record

- The page contains runtime summary, source availability, recent pipeline, anomalies/recovery, and
  diagnostic detail without a separate Memory status console.
- Runtime summary is sourced from public EverOS `/health` and includes `observed_at`.
- Pipeline history reuses existing Avibe processing/call-log provenance.
- Missing/stale/locked/unreadable sources show local `unknown`/`unavailable` states.
- `/health` success is not presented as global pipeline readiness.
- `manual_required` is visible but has no resolution control.
- No private EverOS SQLite dependency, deep scan, new metrics API, or data-rebuild action is required.
- Bodies, secrets, attachments, vectors, and unrelated identifiers are not exposed.
- UI i18n and `npm run build` pass.

### Search

- `keyword`, `vector`, `hybrid`, and explicit `agentic` have provider-neutral adapter tests.
- `hybrid` is the default; `keyword` works without embedding.
- Agentic missing capabilities/budgets fail closed without fallback.
- Search is single-run only; no fan-out or IM alias pairing ships in this PR.
- Profile follows user-global scope and overlays use only the trusted current session.

## 9. Product decision table

| Decision | Final choice |
|---|---|
| Memory data compatibility | New feature; no old-schema inspection, migration, or legacy preservation |
| Delivery shape | One remaining implementation PR into `dev` |
| Provider session identity | Exact `(principal_id, epoch, project_ref, session_id)` `ProviderSessionRef` |
| Flush trigger | Natural add boundary, idle, max-age/message bound, centralized final close, durable boot recovery |
| Flush ordering | Fence first, then drain, flush, exact-token CAS settlement, release next generation |
| Ambiguous provider write | Terminal `manual_required`; retain evidence; no automatic replay |
| Manual resolution | No `operate`, decision rows, or resolution UI in this scope |
| Attachments | Pin/copy before recoverable acceptance; include in backup scope |
| Clear | Destructive, journaled two-phase operation; never a manual-resolution shortcut |
| Processing Record source | Public EverOS `/health` plus existing Avibe processing/call logs and directly confirmed outbox facts |
| Processing Record UI | Implement directly; no separate mockup approval round |
| Global readiness | Not derived or claimed from partial sources |
| Search | Single-run `keyword/vector/hybrid/agentic`; hybrid default; agentic explicit and fail-closed |
| Fan-out | Deferred; no multi-declaration request body |
| IM alias pairing | Deferred |
| Broad user-data rebuild | Out of scope; #1314 separately permits only confirmed embedding-identity recovery |
| EverOS changes | None; no private status or business-metrics API |
| Runtime manifest | Publish/update only with real immutable artifacts and checksums |
| Automatic/generic semantic embedding rotation | Future/gated; #1314 is limited to explicit confirmed identity recovery |

## 10. Primary source references

### Avibe

- `CLAUDE.md` — repository workflow, testing, safety, and PR rules;
- `core/memory/types.py` — canonical provider session and foundation outcome types;
- `core/memory/schema.sql` — clean durable schema merged by PR #1230;
- `core/memory/store.py` — enqueue, claim, settle, recovery, and terminal visibility;
- `core/memory/worker.py` — current durable `/add` delivery path;
- `core/memory/everos.py` — public UDS `/add`, `/flush`, `/health`, `/search`, and `/get` adapter;
- `core/memory/module.py` and `core/memory/runtime.py` — Memory interface and managed lifecycle;
- `core/memory/everos_insight/` — existing processing/call-log provenance adapter;
- `ui/` — existing Processing Record UI, primitives, layout, and i18n;
- `docs/MEMORY.md` — user-facing Memory behavior and recovery notes.

### EverOS

Use the pinned runtime contract and public interfaces already consumed by Avibe. The remaining PR must
not assume new EverOS source changes or private database semantics beyond what the existing Avibe
processing/call-log adapter can safely confirm.
