# Memory MVP: Technical Design

> Status: implementation candidate, pending the phase-0 provider POC
>
> Product contract: `docs/plans/memory-mvp/memory-plugin-everos-phase1.md`
>
> Provider decision: `docs/plans/memory-mvp/memory-plugin-product-research.md`

## 1. Design goal

Build one local vertical slice with a deep `MemoryModule`: a small interface
that hides queueing, provider calls, retries, storage, sidecar lifecycle, and
full clear.

Callers should know only how to submit one capture and how to perform the four
owner operations. They must not know EverOS endpoint states, buffer behavior,
MemCells, Markdown layout, recovery evidence, or provider process details.

No interface or schema in this document is frozen before the phase-0 POC proves
the provider operations and product value.

## 2. Scope

### 2.1 Included

- one local install owner;
- same-origin loopback Workbench access with CSRF;
- user-text-only capture after a committed Workbench message;
- controller-owned local queue and worker;
- provisional EverOS 1.1.3 sidecar;
- direct profile, search, status, and full clear;
- bounded input, response, retry, queue, and disk behavior;
- dedicated Memory state and provider root;
- explicit processing/retention disclosure.

### 2.2 Excluded

- IM and group surfaces;
- Avibe Cloud, LAN, proxy, or other network subjects;
- automatic recall and prompt injection;
- agent-facing tools or backend environment changes;
- assistant-message capture and native-context taint;
- workspace partitioning;
- item deletion;
- export/import;
- foresight and manual Markdown editing;
- provider migration;
- exactly-once provider delivery;
- custom egress relay;
- provider-private database inspection;
- a platform/filesystem support matrix stronger than the tested runtime.

## 3. Module shape

```text
core/memory/
├── __init__.py
├── module.py       # external MemoryModule interface and orchestration
├── types.py        # small caller-facing value types
├── store.py        # dedicated SQLite state; hidden behind MemoryModule
├── worker.py       # bounded queue drain; hidden behind MemoryModule
├── everos.py       # internal EverOS port adapter
└── sidecar.py      # pinned environment and owned process/root lifecycle
```

UI and controller wiring remain outside this module:

```text
vibe/ui_server.py              # local-only Memory routes and capture hook
config/v2_config.py            # MemoryConfig persistence
ui/src/...                     # Memory settings/view
core/memory/migrations/...      # migrations owned by the dedicated Memory store
```

The exact storage location should follow existing state-path helpers. Production
code must derive it from the effective Avibe home and must not reconstruct
`~/.avibe` with `Path.home()`.

## 4. External interface

The interface is product-level. It contains no placeholder for a future
provider or capability.

```python
from dataclasses import dataclass
from typing import Literal


MemoryKind = Literal["profile", "episode", "fact"]
MemoryErrorCode = Literal[
    "memory_disabled",
    "memory_not_owner",
    "memory_invalid_input",
    "memory_input_too_large",
    "memory_queue_full",
    "memory_low_disk_space",
    "memory_store_unavailable",
    "memory_sidecar_unavailable",
    "memory_provider_timeout",
    "memory_provider_response_invalid",
    "memory_processing_failed",
    "memory_clear_failed",
]


@dataclass(frozen=True)
class LocalOwnerGrant:
    subject_fingerprint: str
    issued_at_ms: int


@dataclass(frozen=True)
class CaptureRequest:
    source_message_id: str
    session_id: str
    text: str
    occurred_at_ms: int


@dataclass(frozen=True)
class CaptureAccepted:
    status: Literal["accepted"] = "accepted"


@dataclass(frozen=True)
class CaptureDuplicate:
    status: Literal["duplicate"] = "duplicate"


@dataclass(frozen=True)
class CaptureSkipped:
    reason: MemoryErrorCode
    status: Literal["skipped"] = "skipped"


@dataclass(frozen=True)
class OperationFailed:
    error: MemoryErrorCode
    status: Literal["failed"] = "failed"


CaptureReceipt = (
    CaptureAccepted | CaptureDuplicate | CaptureSkipped | OperationFailed
)


@dataclass(frozen=True)
class MemoryItem:
    kind: MemoryKind
    text: str
    date: str | None = None


@dataclass(frozen=True)
class MemoryItems:
    items: tuple[MemoryItem, ...] = ()
    warnings: tuple[MemoryErrorCode, ...] = ()
    status: Literal["ok"] = "ok"


MemoryResult = MemoryItems | OperationFailed


@dataclass(frozen=True)
class MemoryStatus:
    state: Literal[
        "denied",
        "disabled",
        "starting",
        "ready",
        "indexing",
        "degraded",
        "down",
        "clearing",
        "error",
    ]
    pending: int = 0
    processing: int = 0
    dead: int = 0
    missed: int = 0
    queue_plaintext_bytes: int = 0
    provider_disk_bytes: int = 0
    last_success_at: str | None = None
    error: MemoryErrorCode | None = None


@dataclass(frozen=True)
class ClearCompleted:
    epoch: int
    status: Literal["completed"] = "completed"


ClearReceipt = ClearCompleted | OperationFailed


class MemoryModule:
    async def capture(
        self,
        grant: LocalOwnerGrant,
        request: CaptureRequest,
    ) -> CaptureReceipt: ...

    async def search(
        self,
        grant: LocalOwnerGrant,
        query: str,
        *,
        limit: int = 8,
    ) -> MemoryResult: ...

    async def profile(self, grant: LocalOwnerGrant) -> MemoryResult: ...

    async def status(self, grant: LocalOwnerGrant) -> MemoryStatus: ...

    async def clear(self, grant: LocalOwnerGrant) -> ClearReceipt: ...
```

### 4.1 Interface invariants

- `capture` validates the same fresh server-created loopback owner grant as the
  read methods, then performs local validation and one local queue transaction.
  It never calls the provider or waits on a model endpoint.
- `capture` is idempotent by `source_message_id` for as long as its content-free
  tombstone is retained.
- Client JSON never creates an owner grant. The module accepts only the
  server-created in-process value and checks its subject fingerprint and age.
- `search` and `profile` return bounded inert data. They do not produce chat or
  agent side effects.
- `clear` is idempotent. A crash can delay completion but cannot cause the
  module to open on a partially cleared epoch.
- Errors are closed codes. Raw provider exceptions, URLs, paths containing user
  data, and response bodies never cross the interface.

There is intentionally no `forget`, `recall`, `remember`, `export`,
`capabilities`, `schedule_session_flush`, backend-context method, workspace
field, or caller-owned database connection.

## 5. Internal provider port

EverOS is a true external dependency, so the module has one internal port. The
real adapter and a test fake satisfy it. This port is private to the module and
is not a promise that providers are interchangeable.

```python
@dataclass(frozen=True)
class ProviderCapture:
    principal_id: str
    session_ref: str
    text: str
    occurred_at_ms: int


class MemoryProviderPort(Protocol):
    async def ingest(self, capture: ProviderCapture) -> None: ...

    async def search(
        self,
        principal_id: str,
        query: str,
        limit: int,
    ) -> tuple[MemoryItem, ...]: ...

    async def profile(self, principal_id: str) -> tuple[MemoryItem, ...]: ...

    async def health(self) -> bool: ...
```

`EverOSPort.ingest()` owns the provider-specific add and flush sequence. The
worker sees one success or one closed exception. Provider buffer states,
response unions, and artifact materialization never leak into `worker.py` or
caller-facing types.

If the POC shows that `ingest()` cannot provide a useful stable outcome through
public provider behavior, the decision is to fork or replace EverOS. Core does
not compensate by reading private SQLite or Markdown evidence.

## 6. Identity and scope

The store creates these immutable values on first successful enablement:

- random owner principal UUID;
- random 256-bit scope key;
- random provider-root id stored in an ownership sentinel.

The provider mapping is fixed:

```text
app_id      = "avibe"
project_id  = "personal"
user_id     = principal UUID
session_id  = "wb--" + keyed_digest(local_session_id) + "--e" + epoch
```

The keyed digest is path-safe, bounded, and derived inside the module. Raw
Workbench session ids stay in Avibe state. There is no workspace or platform
dimension in the MVP.

An absent or mismatched provider-root sentinel fails enablement and clear. The
module never adopts an arbitrary nonempty directory as its root.

## 7. Configuration

Add the minimum `MemoryConfig` to V2 config:

```yaml
memory:
  enabled: false
  processing:
    llm:
      base_url: null
      model: null
      api_key: null
    embedding:
      base_url: null
      model: null
      api_key: null
```

Rules:

- Both blocks are required before enablement.
- Keys are write-only in every response; omission preserves the current key and
  explicit clear removes it only while Memory is disabled or being cleared.
- URLs are bounded absolute `http` or `https` URLs. Plain HTTP is allowed only
  for numeric loopback addresses; non-loopback destinations require normally
  verified HTTPS.
- Userinfo, query, fragment, empty model, UI mask values, and oversized fields
  are rejected before save.
- A dedicated loopback-only Memory settings route owns mutations and disclosure.
- The route persists V2 config and asks the live controller to reconcile through
  the existing runtime-refresh pattern. A successful route does not issue a
  second restart.
- Generic config projection redacts keys. Broader config-writer concurrency
  hardening, if needed, is a separate Avibe infrastructure task rather than a
  Memory-specific state machine.

Queue, input, retry, response, and disk limits are internal constants in the
MVP. They are not part of `MemoryConfig` until users have a demonstrated need to
tune them.

## 8. Dedicated Memory store

Memory operational state lives in a dedicated SQLite database under the
effective Avibe state directory. This keeps Memory lifecycle local and avoids
adding a dozen feature-specific tables to the main chat database.

Two tables are sufficient.

### 8.1 `memory_meta`

```text
memory_meta
├── singleton              INTEGER PRIMARY KEY CHECK (singleton = 1)
├── epoch                  INTEGER NOT NULL
├── lifecycle_state        TEXT NOT NULL
│                            # disabled | starting | enabled | clearing | error
├── principal_id           TEXT NOT NULL
├── scope_key              BLOB NOT NULL
├── provider_root_id       TEXT NOT NULL
├── missed_count           INTEGER NOT NULL DEFAULT 0
├── last_success_at        TEXT
├── last_error             TEXT
└── updated_at             TEXT NOT NULL
```

`last_error` is a closed code only. The row contains no message text, endpoint
key, query, or result.

### 8.2 `memory_capture_queue`

```text
memory_capture_queue
├── source_message_id      TEXT PRIMARY KEY
├── epoch                  INTEGER NOT NULL
├── session_id             TEXT NOT NULL
├── payload_text           TEXT
├── occurred_at_ms         INTEGER NOT NULL
├── state                  TEXT NOT NULL
│                            # pending | processing | delivered | dead
├── attempts               INTEGER NOT NULL DEFAULT 0
├── next_retry_at          TEXT
├── lease_owner            TEXT
├── lease_at               TEXT
├── last_error             TEXT
├── created_at             TEXT NOT NULL
└── completed_at           TEXT

INDEX ix_memory_capture_due (epoch, state, next_retry_at)
```

State rules:

- `pending` and `processing` require a non-null payload.
- `delivered` clears payload immediately and retains a content-free tombstone.
- `dead` retains payload for 14 days for status/debugging, then clears it while
  keeping the tombstone until normal terminal compaction.
- Terminal tombstones may compact after 90 days or at a fixed 100,000-row cap.
  Replaying a source older than that window can enqueue it again; the product
  already promises only bounded local idempotency.
- Clear removes every queue row, including tombstones.

SQLite uses the repository's normal durable settings and transaction helpers.
The MVP does not change all Avibe or provider connections to `synchronous=FULL`,
add directory-fsync barriers, or restrict filesystems to claim a stronger
power-loss contract than the rest of Avibe.

## 9. Capture call path

The Workbench composer remains the only capture source.

```text
POST ordinary Workbench message
    -> existing session and owner authorization
    -> normalize/persist user message through the existing route
    -> after commit, check same-origin loopback and CSRF capture eligibility
         -> ineligible: do not invoke Memory
         -> eligible: create LocalOwnerGrant and CaptureRequest from server fields
              -> await MemoryModule.capture(grant, request)
                   -> validate grant/enabled/current epoch/text/session/id/size
                   -> INSERT OR IGNORE memory_capture_queue
    -> continue ordinary agent dispatch regardless of Memory receipt
```

The hook uses the committed server message id, server session id, normalized raw
user text before framework metadata, and server timestamp. Browser-supplied
principal, epoch, provider ids, or capture flags are ignored.

The hook is placed in the shared Workbench route, not an agent backend. No
change is made to `AgentRequest`, Claude, Codex, OpenCode, message dispatch,
message mirroring, or IM adapters.

Capture validation:

- the grant subject must match the current local owner and its age must be
  within the fixed short validity window;
- Memory config and lifecycle must both be enabled;
- text is normalized to NFC with CRLF/CR converted to LF;
- blank input and Memory route operations are skipped;
- UTF-8 text must be at most 32 KiB;
- ids and session values must satisfy fixed nonblank byte caps;
- queue nonterminal rows must remain below 500;
- total pending/processing plaintext must remain below 64 MiB;
- observed free disk must be at least 512 MiB.

A validation or capacity skip increments only `memory_meta.missed_count`. It
does not retain the rejected text or create attacker-controlled per-cause rows.

There is an accepted crash window between the ordinary message commit and the
separate queue insert. The MVP does not add a cross-module database transaction
or startup history scan to close it. Dogfood measurements determine whether the
window justifies a later shared transactional outbox.

## 10. Worker lifecycle

One controller-owned task drains due rows while lifecycle state is `enabled`.

### 10.1 Claim

In a short transaction, claim one `pending` row whose `next_retry_at` is due by
changing it to `processing`, incrementing `attempts`, and setting the current
boot/task lease. The provider call occurs outside the transaction.

On startup, every `processing` row owned by a prior boot returns to `pending`.
Because the old provider call may have succeeded, this reclaim is explicitly
at-least-once.

### 10.2 Deliver

The worker derives `ProviderCapture` and calls `provider.ingest()`. The EverOS
port performs one add and one flush under bounded deadlines. This avoids a
separate persistent flush queue and backend session-close integration.

On success, one transaction changes the row to `delivered`, clears its payload,
and updates `last_success_at`.

### 10.3 Retry and dead state

The first, second, and third attempts run at initial claim, +30 seconds, and +2
minutes. A retryable failure returns the row to `pending` with the next time. A
third failure or a closed non-retryable error changes it to `dead`.

`last_error` contains only a closed category such as:

- `memory_sidecar_unavailable`;
- `memory_processing_failed`;
- `memory_provider_timeout`;
- `memory_provider_response_invalid`;
- `memory_queue_full`;
- `memory_low_disk_space`.

There is no automatic fourth attempt, evidence reconciliation, subset replay,
repair fence, owner drain, or provider-private read. Re-enable may resume
`pending` rows but does not re-arm `dead` rows. Clear is the MVP recovery for
unwanted dead/pending content.

## 11. Read path

### 11.1 Authorization

The UI route proves all of the following before creating `LocalOwnerGrant`:

- direct same-origin request;
- TCP peer classified as loopback by the existing trusted request helper;
- valid session authentication;
- valid CSRF cookie/header pair;
- no proxy/forwarded-header path accepted as loopback authority.

`MemoryModule` verifies the fresh grant fingerprint and age again before
provider access. This is defense against accidental internal route misuse, not a
sandbox against same-user code.

Grant creation is independent of Memory lifecycle state. `capture`, `search`,
and `profile` require enabled Memory; `status` and `clear` remain available when
disabled so an owner can inspect retained state or clear it before re-enabling.

### 11.2 Bounds

Use fixed limits:

- normalized query: 8 KiB;
- search limit: 1 to 20, default 8;
- sidecar response body: 2 MiB;
- complete item text: 64 KiB;
- complete result: 256 KiB;
- total explicit read deadline: 20 seconds;
- profile/search provider item kinds: profile, episode, fact only.

The adapter streams and counts the response before decoding. Invalid JSON,
wrong type, unexpected kind, excessive depth/count, non-finite values, control
characters in identifiers, or a limit crossing returns a closed error. It never
returns a partially decoded raw provider body.

### 11.3 Mapping

The EverOS port maps public response fields deterministically:

- profile -> one canonical text item;
- episode -> bounded subject/summary/content text and valid date;
- fact -> bounded fact text and valid date when present.

The MVP does not open Markdown files to verify each HTTP item, expose opaque
provider refs, or claim per-item source links. A provider response that cannot
be mapped safely is omitted with a closed warning or fails the whole read when
the envelope is invalid.

## 12. Sidecar manager

The sidecar implementation remains deliberately small.

### 12.1 Paths

```text
<AVIBE_HOME>/memory/
├── env-everos-1.1.3-<lock-id>/  # immutable pinned runtime
├── everos-root/                  # provider data; sentinel owned
├── .rt/everos.sock               # runtime socket in mode 0600
└── generated/                    # mode 0700; generated config files 0600
```

The environment is never inside `everos-root`. Clear removes provider data, not
the runtime. Staging environment creation uses a random sibling and atomic
rename after package/version/import verification.

### 12.2 Process

- Require a supported Python 3.12 path and exact dependency lock selected by the
  POC.
- Launch the provider as an owned child with an owner-only working home and
  minimal environment.
- Bind the provider only to a Unix-domain socket in an owner-only directory.
- Never expose the socket through remote access or a TCP listener.
- Disable body access logs and do not persist raw sidecar stdout/stderr.
- Validate required mode, project, timezone, and text-only adapter settings
  before capture opens.
- Pass the configured processing endpoints and keys only through owner-only
  generated config/environment needed by the pinned provider.
- Treat the configured processing endpoints as trusted destinations for the
  experimental MVP. The POC records redirects and egress; a stricter relay or
  fork is a later decision if evidence requires it.
- Stop only the PID started by this manager. Never adopt or kill an unknown
  process found at a path.

The UDS removes the upstream wildcard-CORS browser network path. Same-user local
code can still open the socket or read files and is inside the stated desktop
trust model.

## 13. Clear lifecycle

The module owns one async lifecycle lock shared by enable, disable, clear, and
sidecar restart.

Clear sequence:

1. acquire the lifecycle lock;
2. persist `lifecycle_state=clearing`, increment `epoch`, and reset
   `missed_count`, `last_success_at`, and `last_error` in one transaction;
3. stop new worker claims and wait a bounded time for the current task;
4. stop and prove exit of the owned sidecar;
5. verify the exact expected root path, owner, type, and root-id sentinel;
6. remove root children with no-follow traversal;
7. delete every queue row and recreate the empty sentinel-owned root;
8. if config remains enabled, start/health-check the sidecar and persist
   `enabled`; otherwise persist `disabled`;
9. on restart failure, persist `error` while leaving the old epoch cleared.

Startup sees `clearing` before starting any worker or sidecar and resumes at step
3. All remaining steps are idempotent. The operation needs no durable action
receipt, recovery matrix, backup discovery, or export cut.

The UI confirmation is fresh interaction plus the ordinary CSRF proof. It is
not persisted across restart; an exact retry simply performs the same idempotent
clear again.

## 14. HTTP routes

All routes are same-origin loopback-only and use native async FastAPI handlers.
They await the `MemoryModule` interface directly. Blocking SQLite and filesystem
work inside the module runs through the repository's bounded threadpool
convention; request paths never use per-request `asyncio.run()` bridges.

```text
GET   /api/memory/settings
PATCH /api/memory/settings
GET   /api/memory/status
GET   /api/memory/profile
POST  /api/memory/search
POST  /api/memory/clear
```

Rules:

- Every response containing Memory content uses `Cache-Control: no-store`.
- Search/profile/status/clear never create an ordinary message or agent turn.
- The search body contains only bounded query and limit fields.
- Clear requires an explicit confirmation boolean/string accepted only after the
  UI modal and CSRF checks.
- Settings responses expose `has_api_key`, never the key or reusable mask.
- No endpoint accepts principal, provider project, provider root, session ref,
  local path, scope key, epoch, socket path, or owner grant from client JSON.

The UI uses text nodes for provider content. It does not use raw HTML rendering
or pass results through generic Markdown/directive processing.

## 15. Status semantics

Precedence:

1. invalid owner grant -> `denied` with zero counters;
2. persisted `clearing` -> `clearing`;
3. persisted `starting` -> `starting`;
4. config disabled or persisted disabled -> `disabled`;
5. lifecycle error -> `error`;
6. sidecar unexpectedly unreachable -> `down`;
7. any dead work, capacity/disk pause, or last processing failure -> `degraded`;
8. pending or processing work -> `indexing`;
9. reachable sidecar -> `ready`.

`provider_disk_bytes` is a bounded best-effort traversal of the expected
sentinel-owned root. Failure reports a closed warning and never follows links.
Status does not claim that every provider-owned asynchronous derived track has
succeeded when the public provider interface cannot prove it.

## 16. Logging and errors

Memory-owned code may log:

- request/queue ids or keyed digests;
- row counts and byte counts;
- latency;
- process id and exit code;
- HTTP status class;
- closed error category.

It may not log:

- user text, queries, profiles, facts, or episodes;
- API keys or authorization headers;
- full endpoint URLs;
- provider response/error bodies;
- raw session ids or scope keys;
- generated provider config.

Focused tests inject unique canary values through success and failure and assert
that Memory logs and serialized route errors contain none of them. Any broader
existing Avibe logging/Sentry privacy issue is tracked and fixed at its shared
layer; this module does not create a second telemetry framework.

## 17. Test strategy

The interface is the primary test surface.

### 17.1 Module contract tests with a fake provider

- disabled and invalid-owner behavior;
- capture validation and duplicate source id;
- queue row/byte/disk caps;
- worker success, bounded retry, dead state, and payload scrubbing;
- old-boot `processing` reclaim and documented duplicate possibility;
- search/profile bounds and closed errors;
- idempotent clear and interrupted-clear startup recovery;
- status precedence;
- no raw content in logs/errors.

### 17.2 Store tests

- schema/check constraints and indexes;
- atomic claim and concurrent duplicate insert;
- terminal payload clearing and retention compaction;
- epoch change and queue deletion during clear;
- effective-home isolation so tests never write real user state.

### 17.3 Route tests

- loopback + authentication + CSRF required;
- forwarded/proxy/network requests denied;
- keys remain write-only;
- no-store content responses;
- search/profile/clear create no message, SSE, inbox, or agent event;
- provider content renders as inert text;
- accepted config reconcile updates the live controller once.

### 17.4 EverOS integration tests

The phase-0 POC supplies the provider facts. Production integration tests reuse
its synthetic fixtures for:

- pinned package/config startup;
- UDS-only health;
- add+flush through `EverOSPort.ingest()`;
- search/profile mapping and response bounds;
- stop/restart;
- root sentinel and full clear;
- child environment and destination recording.

Production tests do not copy a provider recovery algorithm into a second
harness.

### 17.5 User-facing verification

After implementation:

- run focused pytest files first;
- run `ruff check` on changed Python files;
- run the UI build;
- run the relevant scenario test;
- use the local Incus regression workflow for the Workbench enable -> capture ->
  search/profile -> clear path;
- keep slow broad gates in CI.

## 18. Delivery slices

### Slice 0: provider POC

Run `docs/plans/memory-mvp/memory-poc-everos.md`. Select official EverOS, a small fork, or
another provider. Update only the internal port implementation when the product
interface remains valid.

### Slice 1: storage and module

- caller-facing types and five-method interface;
- dedicated Memory store;
- fake provider and module contract tests;
- queue worker and idempotent clear;
- controller lifecycle shell.

This slice can be tested without EverOS or model credentials.

### Slice 2: local provider and settings

- pinned sidecar manager;
- selected provider port;
- V2 config and live reconciliation;
- loopback-only routes;
- disclosure, status, and clear.

### Slice 3: Workbench vertical path

- committed user-message capture hook;
- profile/search/status UI;
- end-to-end and Incus verification;
- experimental feature flag and dogfood measurements.

No slice adds IM or agent-backend behavior.

## 19. POC-dependent questions

Only these questions may change the implementation candidate:

1. Does official EverOS satisfy quality and resource gates?
2. Is add+flush per capture acceptable in latency and model cost?
3. Can public provider responses safely implement search/profile mapping?
4. Is at-least-once duplicate behavior acceptable for experimental use?
5. Which tested OS/runtime combinations can ship initially?
6. Does the integration need a small fork for stable write, auth, or egress
   behavior?

If an answer requires the old `WriteEvidence`, per-message recovery, backend
taint, remote-owner registry, or cross-process Memory-specific config protocol,
the design does not silently restore it. The provider or MVP scope is revisited.

## 20. Superseded design elements

The following rev37 elements are intentionally removed from the active design:

- provider-neutral future capability surface;
- `workspace_id` and Plan-B switch;
- `forget`, `remember`, automatic `recall`, and export;
- assistant capture and backend native-context taint;
- dispatch-id propagation across all agent backends;
- group/IM/Cloud/network authorization and remote-owner tables;
- direct IM command bypasses;
- provider `WriteEvidence`, affected-source sets, per-message dispositions,
  repair fences, and `awaiting_flush` lifecycle;
- separate outbox/source/operation/export/flush/clock/snapshot/command/action
  tables;
- strong fsync/directory-barrier filesystem contract;
- migration-backup deletion;
- processing transition receipts and custom egress relay;
- foresight Markdown reader and lineage verification;
- 36-round changelog inside the normative document.

Git history remains the record for those investigations. Useful findings can be
reintroduced only when a later scoped capability actually needs them.
