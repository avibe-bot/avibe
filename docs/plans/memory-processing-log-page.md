# Memory Processing Log Page (EverOS step timeline + provider call log)

Status: draft v7 (overdesign reductions incorporated)
Date: 2026-08-04

## Background

The Memory settings page exposes status, profile, search, and settings, but it
does not explain how one captured message became a distilled memory. The new
"Log" tab should answer that question with a memcell list and a detail timeline:

```
capture -> add / flush -> memcell -> episode -> offline strategies -> profile trigger -> indexing
```

When diagnostics are explicitly enabled, the same timeline also shows the raw
LLM, multimodal-LLM, and embedding requests and responses that can be attributed
to those steps exactly.

## Scope and deliberate limits

This first implementation favors exact, fail-closed answers over reconstructing
every possible relationship:

- Avibe creates a separate EverOS session for each principal: the provider
  session id is derived from `(principal_id, project_id, session_id, epoch)`, and
  every `/add` contains exactly one message from that principal. An Avibe memcell
  therefore has exactly one user sender. A foreign, corrupt, or future memcell
  with zero or multiple user senders is omitted from this scoped UI rather than
  introducing multi-owner call ACLs.
- Storage admits only calls made inside a validated `/add` or `/flush` boundary,
  or with an exact run, memcell, or cascade key. Search, Get, and other EverOS API
  work is never recorded. A valid add/flush request that later fails may leave a
  diagnostic row before Avibe has a durable request-id tombstone; that row remains
  invisible to every reader and expires normally. This bounded exception avoids a
  second cross-process request buffer while keeping unrelated user queries off disk.
- EverOS profile extraction may select several memcells while its immutable
  `run_record` stores only the triggering memcell. V1 reports the exact statement
  "this memcell triggered this profile run" and shows the current profile
  separately. It does not claim that a non-trigger memcell was incorporated and
  does not add a second profile-selection audit table just to reconstruct that
  relationship.
- Persisting raw sidecar stdout/stderr is out of scope. EverOS can log response
  content tails, while the existing Clear flow intentionally preserves general
  logs; that needs a separate retention and disclosure decision.
- Avibe pins `memorize.mode='chat'` and disables `extract_foresight`. V1 therefore
  patches only the reachable Episode and AtomicFact cascade paths; Foresight and
  AgentCase wrappers wait until product configuration can actually enable them.

These limits keep the new version-coupled implementation inside one deep adapter
and avoid changing EverOS or adding a parallel event store.

## Verified constraints

1. EverOS runs out of process from the checksummed `everos==1.2.1` runtime pinned
   by `core/memory/artifact.py::EVEROS_VERSION`. Avibe controls the child launcher
   in `core/memory/sidecar.py`, so patches can be installed before the EverOS app
   module is imported.
2. Existing durable data already covers the non-payload timeline:
   - `system.db.memcell` has `memcell_id`, scope, `message_ids_json`,
     `sender_ids_json`, and the archived memcell payload.
   - `ome.db.run_record` has strategy, status, attempt, timing, error, and event
     payloads containing the relevant scope and/or `memcell_id`.
   - Avibe `memory_capture_queue` retains scope, provider session id,
     `provider_timestamp_ms`, add/flush request ids, and terminal delivery state
     after its source text is scrubbed.
   - `md_change_state` and `user.md` are current state, not historical snapshots.
3. The exact capture-to-memcell key can be derived without another queue column.
   Avibe sends one message per `/add`, and pinned EverOS generates
   `m_<session_id>_<timestamp_ms>_<idx:03d>`, so the queue row's provider message
   id is exactly `m_{session_id}_{provider_timestamp_ms}_000`. The versioned
   adapter owns this derivation and a real-wheel contract test pins it.
4. Reliable provider choke points in the pinned wheels are class attributes:
   `OpenAICompatClient.chat` and `OpenAIEmbeddingProvider._embed_chunk`.
   Existing EverOS context supplies `request_id` for synchronous request work and
   `strategy_name` / `run_id` / `attempt` for OME work, but it does not supply
   owner scope. Authorization must therefore follow those ids back to durable
   source rows instead of copying an inferred owner onto every call.
5. The parser consumer imports `everos.component.parser.aparse_file` from the
   package export at call time; patching only `_core.aparse_file` misses it.
   Cascade dispatch calls concrete `_build_row` overrides, not the abstract
   `BaseDailyLogHandler._build_row` method.

## Design

### Versioned adapter

Add `core/memory/everos_insight/`, which owns every pinned-wheel patch, EverOS
sqlite query, event-payload parser, and message-id derivation. Its external
interface stays small:

```python
prepare_call_recorder(db_path) -> RecorderHandle | None
MemoryInsightPaths(everos_root, capture_db_path, call_log_db_path)
MemoryInsightReader(paths).list_entries(scope, cursor, limit) -> dict
MemoryInsightReader(paths).entry_detail(scope, memcell_id) -> dict
```

`MemoryRuntime` constructs the frozen path bundle from its own
`self._provider_root`, the injected/opened `MemoryStore.path`, and its owned
call-log path, then injects one configured reader. The adapter never rediscovers
`~/.avibe`, calls `memory_store_path()`, or reads global path configuration.
This keeps alternate `effective_home` runtimes and hermetic tests confined to
their injected state.

`RecorderHandle.start()` / async `close()` are lifecycle methods, and
`boundary_request()` is the synchronous ContextVar context manager used by the
validated sidecar guard. Only the sidecar launcher sees the handle. Other callers
and tests cross the same configured-reader interface. An
`EVEROS_VERSION` change requires re-verifying this adapter and its real-wheel
contract tests, not scattered callers.

### Authorization model

Both read functions receive `(principal_id, project_id)` from the existing
`_memory_read_scope`. The base memcell must satisfy all of:

- `app_id = 'avibe'`
- `project_id = requested project`
- `sender_ids_json` is a valid JSON array of length one whose only value is the
  requesting principal

Provider calls do not duplicate a supposedly universal scope. The reader proves
authorization through the call's exact provenance:

| Provenance | Authorization proof |
|---|---|
| `request_id` | The call was admitted inside a validated add/flush boundary, and matching Avibe queue rows all belong to the requested principal/project; a mixed or missing group is rejected |
| `memcell_id` | The referenced memcell passes the base singleton-principal check |
| `run_id` | The joined `run_record` event has matching app/project. If `owner_id` is present, it is authoritative and must exactly match the requested principal; only an event with no owner field may fall back to a referenced memcell that passes the base check |
| cascade entry | Captured app/project/owner matches, and `parent_type` / `parent_id` follows an exact path back to this memcell |

If none of those proofs succeeds, the call is omitted. There is no time-window,
session-text, `LIKE`, or "unattributed calls" fallback.

### Privacy model

Raw provider payload capture is opt-in and defaults to off through
`memory.diagnostics.log_provider_calls`. V1 deliberately treats this as an
installation-administrator decision, not per-Cloud-subject consent: only a
direct-loopback Memory request admitted by `is_direct_loopback_memory_request()`
may change this field. An authenticated Avibe Cloud subject may read only its
own authorized rows but receives 403 when a PATCH tries to enable or disable
global capture; the remote UI renders the control read-only. The local toggle
gets explicit English and Chinese disclosure that it captures payloads for all
principals using this installation. Tests prove a remote subject cannot mutate
the field even when the same PATCH changes other Memory settings.

Disabling capture stops new rows; already recorded rows remain readable until
their 14-day expiry or Clear. Because the recorder receives its database path
only when the persisted flag is enabled, the recording boundary independently
enforces the local administrator's choice rather than trusting UI state.

An enabled-to-disabled transition is privacy-first. While holding the lifecycle
lock, `MemoryRuntime` stops the recorder-enabled child and transfers call-log
ownership to host maintenance before artifact resolution, provider health
preflight, or any other fallible replacement work. It then reconciles a child
that never receives the database path. If that replacement cannot start, the
settings response may report Memory degraded, but persisted capture remains off:
rollback restores other failed settings while forcing
`memory.diagnostics.log_provider_calls=False`. Tests hold both provider probes
failed and prove the old recorder is stopped, no replacement receives the path,
and neither runtime nor persisted config re-enables capture.

Recording uses an explicit field whitelist. Every retained string, including
prompt and response text, is recursively scrubbed for bearer/API-key values,
authorization-like fields, configured provider base URLs, and absolute local
paths before it is truncated and stored. SDK objects are never serialized with
`str(obj)`, attachment bytes and embedding vectors are never stored, and all
rendering is inert text/JSON rather than Markdown or HTML.

The database is stored locally, but the UI routes are available to both an
authenticated local UI and an authenticated Avibe Cloud session. Every response
is therefore owner-scoped and `Cache-Control: no-store`; the design does not
describe the data as "local-view-only".

## Phase 1 - Provider call recorder

### Storage

Create `~/.avibe/memory/call-log/call-log.db` with WAL mode,
`auto_vacuum=INCREMENTAL`, `user_version=1`, directory mode 0700, and files mode
0600 after the existing ownership/no-symlink checks.

One table, `provider_call`, is sufficient:

- identity/timing: `id`, `started_at_ms`, `duration_ms`
- result: `kind`, `stage`, `model`, `status`, scrubbed `error`,
  `finish_reason`, `prompt_tokens`, `completion_tokens`
- bounded payloads: `request_json`, `response_json`, and their pre-truncation
  byte counts
- provenance: `request_id`, `strategy_name`, `run_id`, `attempt`, `memcell_id`
- cascade-only provenance: `app_id`, `project_id`, `owner_id`, `md_path`,
  `entry_id`, `parent_type`, `parent_id`
- loss visibility: `dropped_before INTEGER NOT NULL DEFAULT 0`

Index only the reader's lookup keys: `request_id`, `run_id`, `memcell_id`,
`started_at_ms DESC`, and `(parent_type, parent_id)`. Scope columns are nullable
and trusted only for a call captured inside a concrete cascade-entry wrapper;
other calls are authorized through their source rows.

Capture budgets:

- LLM request: 16 KB per message and 64 KB total; retain first/last messages and
  insert an explicit omission marker. Store only the response schema name.
- LLM response: 64 KB content cap plus model, finish reason, and usage.
- Multimodal parts: replace strings over 4 KB with an `omitted_bytes` marker;
  never retain raw attachment bytes.
- Embedding request: model/dimensions/input count and the first 2 KB of the first
  16 inputs. Response stores vector count, dimension, and usage, never vectors.
- Error text: scrubbed and capped at 4 KB.

### Patch targets and context

Install patches before importing `everos.entrypoints.api.app`:

1. `OpenAICompatClient.chat` records main and multimodal LLM calls.
2. `OpenAIEmbeddingProvider._embed_chunk` records all embedding transport calls.
3. `user_memory._extract_with_retry` binds `stage='episode_extract'` and
   `memcell_id` around its transport call.
4. Patch the package export `everos.component.parser.aparse_file` to bind
   `stage='parse'`. Do not patch only `_core.aparse_file`.
5. In the validated sidecar guard, bind an add/flush-only ContextVar around
   `call_next`. Request-id provenance is accepted only while that marker is set;
   valid Search/Get requests do not set it.
6. Apply one wrapper implementation to the reachable concrete class attributes
   `EpisodeHandler._build_row` and `AtomicFactHandler._build_row`. Each wrapper
   binds `stage='cascade'` plus the method's app/project/owner/md path and the
   parsed entry's `entry_id`, `parent_type`, and `parent_id`.

Stage precedence is explicit: parser / episode / cascade bindings win, then a
present `run_id` means strategy, then a present request id plus the admitted
add/flush marker means boundary; otherwise the completed call is not enqueued. A
parse binding also identifies the otherwise indistinguishable multimodal client
kind.

The real-wheel contract test exercises the production call paths, not just the
named attributes: parser enrichment through the package export, the Episode and
AtomicFact `_build_row` paths, episode extraction, an OME-context call, and the
two transport classes. It also drives Search and Get through the real app and
asserts that neither adds a diagnostic row.

### Async lifecycle and failure behavior

`prepare_call_recorder` installs the synchronous wrappers and returns a handle;
it does not start the worker because `sidecar.serve()` has not entered the ASGI
lifespan yet. After `create_app()`, the launcher wraps the existing
`app.router.lifespan_context` with an async context manager. The wrapper starts
the handle, delegates to and yields the original EverOS lifespan state unchanged,
then executes `await handle.close(timeout=1.0)` in `finally`. `close` is async:
it requests a bounded drain and awaits the worker's join through
`asyncio.to_thread`, so ASGI shutdown never blocks its own event loop. The worker
uses a shorter SQLite busy timeout and the same shutdown deadline; at the
deadline it rolls back the active batch, discards the remaining in-memory tail,
closes SQLite on its owning thread, and acknowledges termination. Start/close
failures remain diagnostic-only. This is a lifespan wrapper, not
`add_event_handler`, because the pinned app already installs a custom lifespan.

Transport wrappers only whitelist, scrub, truncate, and append a completed row
to a thread-safe 256-item in-memory queue without waiting. A dedicated writer
thread creates and exclusively owns the SQLite connection and batches inserts,
checkpoints, and pruning; no synchronous SQLite operation runs on the sidecar's
ASGI event-loop thread. On overflow the producer drops the oldest row and puts
the accumulated loss count in the next stored row's `dropped_before`; the UI
renders that as a gap notice. Database waits never run on the provider call path.

Recorder errors are swallowed, while the provider call's original result or
exception is preserved exactly. After 20 consecutive writer failures, recording
self-disables for that process. `RecorderHandle` exposes the closed health states
`active`, `degraded`, and `disabled` through the sidecar health response; degraded
includes a stable reason such as `writer_failures`, never raw exceptions. The
controller projects that state once through the existing Memory status payload.
The Log panel consumes the page's existing status read and reuses the existing
Memory runtime restart action; list/detail responses and routes do not duplicate
health or recovery state. Restarting the sidecar is the recovery operation for a
transient writer failure when the persisted flag remains enabled. Tests cover a
held sqlite write lock, the failure threshold through the single status surface,
recovery through the existing restart action, queue overflow, original exception
identity, original EverOS lifespan delegation/state, startup failure isolation,
bounded shutdown, and shutdown tail persistence.

### Retention, corruption, and Clear

The retention contract is 14 days and at most the newest 5000 calls. A 128 MB
sum of DB + WAL + SHM is a soft safety target, not a guaranteed postcondition of
SQLite file layout. Pruning deletes oldest rows below 5000 when needed, then runs
a bounded incremental-vacuum/checkpoint/remeasure loop; it never blocks a model
call to force physical compaction.

There is one sqlite writer owner at a time:

- while a recorder-enabled sidecar is running, its writer performs pruning;
- while no recorder-enabled child exists and the database does, a host
  `_call_log_retention_loop` prunes once immediately and then about every six
  hours through `asyncio.to_thread`, holding the Memory lifecycle lock used by
  Clear. This includes a normal Memory sidecar running with diagnostics off;
  that child never receives the DB path and cannot have the file open. No task
  is created for an install that has never enabled capture. After acquiring the
  lock, each tick rechecks that no recorder-enabled child owns the DB before
  opening it;
- under that same lock, a lifecycle transition cancels and awaits the host task
  before starting a recorder-enabled sidecar;
- runtime shutdown cancels and awaits the host task.

Neither process unlinks a database that the other may have open. Corruption causes
the active recorder or host maintenance task to stop opening the call log and
report stable `call_log_corrupt` health through Memory status. V1 does not
quarantine, rotate, or automatically recreate a corrupt diagnostic database. The
owned files remain in place until an administrator uses the existing Clear flow;
subsequent restarts remain degraded while those files are corrupt. This keeps
corruption handling fail-closed without introducing a second retention system.
Tests cover sidecar and host detection, stable degraded health across restart,
and recovery only after Clear.

The existing Clear flow first stops any Memory sidecar and serializes with host
maintenance. After verifying the fixed directory's owner, mode, and no-symlink
chain, it lstat/unlinks only regular owned files on a strict allowlist:
`call-log.db` and its WAL/SHM/rollback-journal names. Unexpected entries are
preserved; the directory is removed only if it is then empty. This is
intentionally not a recursive delete. The DB is recreated only when capture is
enabled again.

### Integration and capture tests

- `core/memory/sidecar.py`: prepare patches before the EverOS app import and
  attach the returned handle to ASGI lifespan.
- `core/memory/process.py`: pass `AVIBE_MEMORY_CALL_LOG_DB` only when the
  diagnostic flag is enabled; prepare the owned directory.
- `config/v2_config.py` and the settings route/UI: add the nested default-off
  boolean, PATCH validation, local-only toggle, disclosure, and the asymmetric
  rollback rule that never restores capture after a disable request.
- `core/memory/runtime.py`: treat a diagnostics-flag change as a sidecar
  environment reconciliation, switch recorder/host retention ownership under the
  lifecycle lock, add recorder health to the existing Memory status, and own Clear
  deletion. The settings API relies on that one reconciliation and does not issue
  a second restart. A disable transition revokes the recorder before preflight;
  failures can roll back other candidate fields but not the disabled diagnostics
  flag.

`tests/test_memory_call_log.py` covers serialization, recursive redaction across
all columns, truncation, vectors/attachments omitted, provenance/stage capture,
Search/Get non-capture, `dropped_before`, failure isolation, lifecycle,
enabled-to-disabled retention ownership with failing provider preflight,
single-surface recorder health/recovery, corruption remaining degraded until
Clear, and Clear preserving an unexpected file. Real-wheel tests may skip on an
ordinary developer machine only when the managed artifact is absent. With
`AVIBE_REQUIRE_MEMORY_RUNTIME_CONTRACT=1`, absence, identity mismatch, or any
skipped contract case is a hard failure.

## Phase 2 - Read path and routes

`reader.py` uses short-lived read-only `mode=ro` connections with
`busy_timeout=2000` for EverOS and call-log sqlite files. The synchronous reader
runs via `asyncio.to_thread` while holding the existing async Memory lifecycle
guard. The runtime creates a task for the thread await and shields it from request
cancellation. If the request is cancelled, it records that cancellation, awaits
the reader task to completion while still holding the guard, closes every
short-lived connection, releases the guard, and only then re-raises
`CancelledError`. Clear therefore cannot unlink or replace any database while a
cancelled reader still holds or may open it. A missing, expired, malformed, or
locked section degrades to an explicit "unavailable" step instead of failing the
whole page.

EverOS JSON columns are join inputs, not response objects. The reader constructs
every step from a strict output whitelist: ids, labels, status, timing, scope,
4 KB fully scrubbed errors, and an owner-authorized text preview capped at 512
UTF-8 bytes. Provider-call serialization and timeline projection share one
recursive scrubber for bearer/API-key values, authorization-like field values,
configured provider base URLs, and absolute local paths before applying field
caps. Attachments contribute only type plus a sanitized, 128-byte basename
placeholder. It never returns raw `memcell.payload_json`,
`run_record.event_payload`, an embedded MemCell, absolute URI/path fields, or
stored `md_path`. Provider request/response bodies come only from the already
scrubbed diagnostic table.

Exact joins:

| Link | Join |
|---|---|
| memcell list | app/project match and singleton `sender_ids_json` equals principal |
| memcell -> capture | intersect `memcell.message_ids_json` with derived `m_{queue.session_id}_{queue.provider_timestamp_ms}_000` |
| capture -> add/flush calls | exact add/flush `request_id`; validate every matching queue tombstone has the same requested scope |
| memcell -> episode call | exact `provider_call.memcell_id` after base memcell authorization |
| memcell -> OME runs | exact `json_extract(event_payload, '$.memcell_id')`; validate event app/project, require an explicit owner to match, and use memcell ownership only when the event has no owner field |
| run -> strategy calls | exact authorized `run_record.run_id = provider_call.run_id` |
| profile trigger | authorized `extract_user_profile` run whose trigger event names this memcell; label it as a trigger, not proof of batch inclusion |
| direct cascade -> memcell | cascade `parent_type='memcell' AND parent_id=:memcell_id` plus captured scope |
| atomic-fact cascade -> memcell | cascade parent episode-entry id -> exact `EpisodeExtracted.episode_entry_id` event for this memcell and owner |
| current indexing/profile | `md_change_state` / `user.md`, explicitly labeled current state |

If a queue tombstone has already expired, the capture/delivery section says so;
the reader never falls back to payload text or a shared flush id.

Public adapter results:

- `list_entries(scope, cursor, limit)`: newest-first memcells with preview,
  message count, run summary, and authorized call count. The cursor is an opaque
  URL-safe base64 encoding of `(timestamp_ms, memcell_id)` with length, charset,
  shape, and ordering validation. Tests call this malformed/structural rejection,
  not tamper detection; the cursor is intentionally unsigned because every query
  still reapplies scope.
- `entry_detail(scope, memcell_id)`: ordered steps with authorized calls and
  current-state labels. It returns at most the newest 20 call details and newest
  50 run/strategy timeline rows, with separate `omitted_call_count` and
  `omitted_step_count` values. Projection is a single bounded pass: each returned
  request and response field has a 12,000-byte JSON-encoded representation cap,
  each returned error is capped at 1,024 UTF-8 bytes, and every other string uses
  its declared field cap. A field that exceeds its cap becomes an explicit
  `omitted_bytes` excerpt marker. These fixed collection and field bounds keep the
  worst-case route envelope below 1,000,000 encoded bytes without repeatedly
  serializing and shrinking the whole response. A route-level maximum-input test
  asserts that bound. No unresolved call bucket is returned.

Routes:

- `core/memory/runtime.py`: `log_entries_payload` / `log_entry_payload`.
- `core/internal_server.py`: `GET /internal/memory/log` (`limit` 1..50,
  cursor <= 88 chars) and `GET /internal/memory/log/entry` (validated memcell id),
  using `_memory_read_scope` exactly like profile.
- `vibe/internal_client.py`: signed `memory_log` / `memory_log_entry` helpers
  carrying the user-key headers.
- `vibe/ui_memory_routes.py`: matching `/api/memory/log` routes using the user-key
  guard, `no-store`, `_memory_internal_response`, and native dispatch. Malformed
  parameters return 400 `memory_invalid_input`; a foreign or absent memcell
  returns 404 `memory_log_entry_not_found` without revealing which case occurred.
  Recorder recovery reuses `POST /api/memory/runtime/restart` and its existing
  authorization and internal reconciliation; this feature adds no second restart
  route or client action.

Successful responses use `{"status":"ok", ...}`. Failure envelopes follow the
existing Memory vocabulary and never persist the new request-only not-found code
as `last_error`.

Read-path tests include:

- two principals in one project with disjoint results;
- a synthetic multi-owner memcell omitted for every principal;
- exact derived message-id attribution and rejection of shared-flush shortcuts;
- request groups with mixed scope rejected fail-closed;
- an explicit mismatched run-event owner rejected even when its referenced
  memcell belongs to the requester, ownerless-event fallback, and trigger-only
  profile wording;
- direct and atomic-fact cascade parent chains;
- malformed event JSON, cursor structure/order, exact response-byte cap, expired
  queue row, missing/locked DB, many maximum-error run rows, and Clear
  serialization;
- cancellation after the reader thread starts, proving Clear waits for that
  thread and all its connections before deleting owned files;
- secret-bearing `run_record.error` and event-error fixtures proving the full
  provider-secret/base-URL/path scrubber runs before projection;
- raw event/memcell payloads containing large text, attachment URIs, and absolute
  paths never projected into list/detail responses;
- local authenticated access, authenticated Avibe Cloud access, wrong-subject
  denial, `no-store`, signed internal-client path/query behavior, and invalid
  parameter handling.

## Phase 3 - UI

Add `log` to `MemoryTab` and render `MemoryLogPanel` in the existing manage
stage. Like Profile and Search, the Log tab is intentionally unavailable while
Memory itself is disabled. "Timeline available when payload logging is off"
means the diagnostic toggle does not hide the normal timeline while Memory is
enabled; it does not override the page's disabled setup state.

Keep the five-tab `SegmentedRadio` unchanged. Wrap this page's tab row in a local
`overflow-x-auto` container so other segmented controls do not change behavior.
A unit test asserts the overflow contract; actual narrow-width clipping is
checked in the browser/Incus verification because jsdom has no layout engine.

`MemoryLogPanel` behavior:

- list via `useMemoryResource`, explicit refresh, no polling, and cursor-based
  load-more;
- in-tab detail/back state with a vertical step timeline;
- expandable provider calls showing kind, stage, model, duration, usage, finish
  reason, request/response, copy action, omitted markers, and dropped-call gaps;
- request/response JSON parsed defensively. Reuse `JSON_TREE_MAX_BYTES` and
  `JSON_TREE_MAX_NODES` from `ui/src/lib/filePreview.ts`; over-limit or invalid
  JSON falls back to an inert `<pre>` string instead of mounting an unbounded
  `PreviewJson` tree;
- all provider/user text rendered without Markdown or HTML;
- an explicit current logging-off notice. When the persisted flag is enabled but
  recorder health is `degraded`, show that new provider calls are not being
  recorded. The panel receives the existing Memory status and restart callback
  from `SettingsMemoryPage`; transient recorder failures use the existing runtime
  restart action, while `call_log_corrupt` directs the administrator to Clear.
  For an older step with no provider rows, use the neutral wording "not recorded
  or expired"; do not invent toggle history. Also label unavailable sections and
  current-state-only profile/indexing information.

Add `jsdom`, `@testing-library/react`, and `@testing-library/user-event` as
explicit dev dependencies and use a per-file Vitest jsdom environment. They are
not currently project dependencies. DOM tests cover load-more, detail/back,
expand/collapse, refresh, slow-then-fast request supersession, degraded recorder
state, existing runtime restart recovery, and corrupt-log Clear guidance. Pure
helpers cover cursor accumulation, JSON guards, and view-model shaping. Static SSR tests
continue to cover empty, loading, failure, and forbidden render states. The Log
tests stub the existing status/restart props rather than a recorder-specific API.

All new user-facing strings live in both i18n catalogs, including the diagnostic
disclosure that retained rows survive turning the toggle off until expiry/Clear.
Add `docs/MEMORY.md` and `docs/MEMORY_ZH.md`, linked from the README Docs section,
covering what provider payload capture records and omits, local-administrator-only
enablement, owner-scoped Cloud reads, the 14-day/5000-row retention contract,
retention after disabling, degraded-recorder recovery, and the destructive scope
of Clear.

## Verification

1. Focused Python tests:
   `tests/test_memory_call_log.py`, `tests/test_memory_insight.py`,
   `tests/test_memory_sidecar.py`, `tests/test_memory_everos.py`,
   `tests/test_memory_runtime.py`,
   `tests/test_internal_server.py`, `tests/test_internal_client.py`,
   `tests/test_ui_memory_routes.py`, and `tests/test_memory_config.py`.
2. `ruff check` on changed Python files; `cd ui && npm test -- MemoryLogPanel`
   followed by `npm run build`.
3. Reader contract fixture from a snapshot copy of a real `everos-root` plus
   real-wheel patch-path tests. Add a required `memory-insight-contract` PR job
   to `.github/workflows/lint.yml`: pin `uv==0.9.18`, create an isolated Python
   3.12 environment directly from the checked-in `scripts/memory_runtime/uv.lock`
   with `uv sync --frozen`, point the tests at that interpreter, set
   `AVIBE_REQUIRE_MEMORY_RUNTIME_CONTRACT=1`, and run the private-parser,
   cascade, episode, and transport contract cases unsharded. The job fails when
   the runtime cannot be provisioned or a contract test skips; the ordinary
   unit-test shards remain artifact-independent. Deployable runtime archive and
   metadata verification stay in the existing release workflows rather than
   being repeated on every PR.
4. Incus regression only, never the local `vibe` service: enable diagnostic
   capture, send a message with and without an attachment, wait for flush/OME,
   inspect the list/detail calls, turn capture off and confirm retained rows are
   still visible, verify a second principal is isolated, and check the tab at the
   narrowest supported viewport. Cross-check call-log and run-record counts.

## Touched files

New:

- `core/memory/everos_insight/{__init__,recorder,patches,reader}.py`
- `tests/test_memory_call_log.py`, `tests/test_memory_insight.py`
- `ui/src/components/settings/memory/MemoryLogPanel.tsx`
- `ui/src/components/settings/memory/MemoryLogPanel.test.tsx`
- `docs/MEMORY.md`, `docs/MEMORY_ZH.md`

Edited:

- `core/memory/{everos,sidecar,process,runtime,types}.py`
- `config/v2_config.py`
- `core/internal_server.py`, `vibe/internal_client.py`,
  `vibe/ui_memory_routes.py`
- `ui/src/context/ApiContext.tsx`
- `ui/src/components/settings/SettingsMemoryPage.tsx`
- `ui/src/components/settings/memory/MemorySettingsPanel.tsx`
- `ui/src/i18n/en.json`, `ui/src/i18n/zh.json`
- `ui/package.json`, `ui/package-lock.json`
- `README.md`, `.github/workflows/lint.yml`
- `tests/test_memory_sidecar.py`, `tests/test_memory_runtime.py`,
  `tests/test_memory_everos.py`, `tests/test_internal_server.py`,
  `tests/test_internal_client.py`,
  `tests/test_ui_memory_routes.py`, `tests/test_memory_config.py`
