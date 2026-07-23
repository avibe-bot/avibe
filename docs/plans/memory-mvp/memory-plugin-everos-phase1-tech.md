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
owner operations. The UI, Workbench command, private-IM command, and CLI adapters
reuse those operations rather than extending the module. They must not know EverOS
endpoint states, buffer behavior, MemCells, Markdown layout, recovery evidence,
or provider process details.

No interface or schema in this document is frozen before the phase-0 POC proves
the provider operations and product value.

Memory is a built-in Avibe capability like Vaults, not an App Library app or a
general plugin. Its external runtime is one managed dependency surfaced as
`memory-runtime` under `/admin/settings/dependencies`.

### 1.1 Read this design in one minute

The implementation has one main path:

```text
eligible user message
    -> save one row in Avibe's local Memory queue
    -> return to normal chat immediately
    -> background worker sends that row to the local EverOS process
    -> EverOS calls the configured models and writes its own local data
    -> Memory page, /memory, and vibe memory read the result
```

The local EverOS process is called the **sidecar**. Avibe starts it, checks it,
and restarts it when it crashes. The **provider** is the memory engine behind the
module; EverOS is the provisional provider. A **closed error code** is a short
Avibe-defined category such as `memory_provider_timeout`, never a raw exception
or response body.

There are two kinds of failure and they must not be mixed:

- **system outage:** the sidecar or a configured model endpoint is unavailable;
  pause queue processing and keep every row pending;
- **message failure:** the system is reachable but one row still cannot be
  processed; retry only that row, then erase its text after the retry limit.

## 2. Scope

### 2.1 Included

- one install-level Memory principal and pool, with enabled administrator-DM
  identities treated as co-owners;
- same-origin loopback Workbench access with CSRF;
- one text-only Workbench `/memory` command;
- private-IM user-text capture and the same read-only `/memory` grammar for
  bound, enabled administrator co-owners;
- local `vibe memory status|profile|search` over the existing controller UDS;
- user-text-only capture after an accepted Workbench or eligible private-IM
  message;
- controller-owned local queue and worker;
- Avibe-managed, platform-qualified `memory-runtime` dependency;
- provisional EverOS 1.1.3 sidecar;
- direct profile, search, status, and full clear;
- bounded input, response, retry, queue, and disk behavior;
- dedicated Memory state and provider root;
- explicit processing/retention disclosure.

### 2.2 Excluded

- group IM, non-administrator, unbound, or disabled DM surfaces;
- Avibe Cloud, LAN, proxy, or other network subjects;
- automatic recall and prompt injection;
- registered agent-facing Memory tools and their MCP, OS-enforced
  turn-capability, and backend-registration plumbing; the existing read-only
  CLI may be advertised only on eligible interactive owner turns;
- write-capable commands or CLI subcommands;
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
├── artifact.py     # thin specialization of the shared managed-runtime Module
└── process.py      # Memory-private EverOS child/socket/root lifecycle
```

UI and controller wiring remain outside this module:

```text
vibe/ui_server.py              # auth/presentation, /memory intercept, capture hook
vibe/api.py                    # shared Dependencies status/install-job adapter
config/v2_config.py            # MemoryConfig persistence
ui/src/...                     # Memory settings/view
core/controller.py             # shared IM /memory registration + owner admission
core/handlers/message_handler.py # shared private-IM capture seam
core/internal_server.py        # authorized controller-owned MemoryModule ingress
vibe/internal_client.py        # UI / command / CLI UDS wrappers
vibe/cli.py                    # vibe memory status/profile/search
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
    "memory_invalid_input",
    "memory_input_too_large",
    "memory_queue_full",
    "memory_low_disk_space",
    "memory_store_unavailable",
    "memory_runtime_missing",
    "memory_runtime_unsupported",
    "memory_runtime_install_failed",
    "memory_sidecar_unavailable",
    "memory_provider_timeout",
    "memory_provider_response_invalid",
    "memory_processing_failed",
    "memory_clear_failed",
]


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
        request: CaptureRequest,
    ) -> CaptureReceipt: ...

    async def search(
        self,
        query: str,
        *,
        limit: int = 8,
    ) -> MemoryResult: ...

    async def profile(self) -> MemoryResult: ...

    async def status(self) -> MemoryStatus: ...

    async def clear(self) -> ClearReceipt: ...
```

### 4.1 Interface invariants

- Entry adapters authorize before crossing the Interface. `capture` then performs
  local validation and one local queue transaction; it never calls the provider
  or waits on a model endpoint.
- `capture` derives a scope-keyed digest from `source_message_id` and is
  idempotent by that digest for as long as its content-free tombstone is retained.
- Client JSON and CLI arguments never supply principal, provider, root, epoch, or
  lifecycle values. Controller-owned adapters derive those values before calling
  the module.
- `search` and `profile` return bounded inert data. The module never dispatches
  chat or an agent turn; authorized entry adapters return the result only to the
  UI, Workbench command panel, private IM response, or CLI.
- `clear` is idempotent. A crash can delay completion but cannot cause the
  module to open on a partially cleared epoch.
- Errors are closed codes. Raw provider exceptions, URLs, paths containing user
  data, and response bodies never cross the interface.

Authorization is intentionally not represented by a same-process capability
object. Workbench, IM, and CLI already have distinct trusted entry checks; a
token created and rechecked inside the controller would not establish another
security seam. Unauthorized callers are rejected before `MemoryModule` is
invoked.

There is intentionally no `forget`, `recall`, `remember`, `export`,
`capabilities`, `schedule_session_flush`, backend-context method, workspace
field, or caller-owned database connection.

The Memory page plus the three explicit read adapters call only `search`,
`profile`, and `status`. They do not add methods, tables, provider operations, or
a second source of bounds and error semantics. `clear` remains UI-only and
`capture` remains automatic.

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
    provider_timestamp_ms: int


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

EverOS derives provider message ids from the session, timestamp, and request
index, and deduplicates only within the current buffer. Avibe therefore assigns
each new queue row one `provider_timestamp_ms` in the same transaction that
inserts the row. It is `max(occurred_at_ms, last_provider_timestamp_ms + 1)` and
is stored on the row. Every retry and process restart reuses that exact value.
The original `occurred_at_ms` remains the real message time and is never changed
for provider bookkeeping.

The singleton `last_provider_timestamp_ms` is global rather than per session.
That is stricter than EverOS requires and avoids restoring rev37's clock table.
Only a newly accepted, distinct source digest advances it; a duplicate insert
does not allocate another value. Timestamps outside the provider-safe range
(the pinned version overflows on far-future values in positive-offset
timezones) fail with a closed error instead of being forwarded.

If the POC shows that `ingest()` cannot provide a useful stable outcome through
public provider behavior, the decision is to fork or replace EverOS. Core does
not compensate by reading private SQLite or Markdown evidence.

## 6. Identity and scope

The store creates these immutable values on first successful enablement:

- random owner principal UUID;
- random 256-bit scope key;
- random provider-root id stored in an ownership sentinel.

The sentinel is created atomically with a new provider root and contains only:

- `schema_version`, for the sentinel record itself;
- `provider_root_id`, which must equal `memory_meta.provider_root_id`;
- `provider_id`, fixed to `everos` for this MVP;
- `provider_root_format`, copied from the active runtime manifest; and
- `created_by_artifact_fingerprint`, identifying the exact runtime artifact that
  initialized the root.

`provider_root_format` is the upgrade compatibility key. The artifact
fingerprint is diagnostic provenance, not a reason by itself to reject a
compatible upgrade. These fields contain no endpoint, key, user text, or source
identity.

The provider principal mapping is fixed while the session reference is
source-neutral:

```text
app_id      = "avibe"
project_id  = "personal"
user_id     = principal UUID
session_id  = "src--" + keyed_digest(source_kind, source_conversation_id) + "--e" + epoch
```

The keyed digest is path-safe, bounded, and derived inside the module. Workbench
uses its local session identity; private IM uses the platform plus its canonical
DM conversation identity. Raw Workbench session ids and raw IM platform, user,
or chat ids stay in Avibe state. The provider still sees one principal and one
project: the source qualifier prevents session collisions, not per-platform or
per-user memory partitioning.

If the provider-root path already exists, an absent or mismatched sentinel fails
enablement and clear. A fresh install with no root is allowed; first enablement
creates the root and sentinel atomically. The module never adopts an arbitrary
existing directory as its root.

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
- Enablement performs one bounded authenticated probe against the configured LLM
  service and one against the embedding service. Invalid credentials, model
  names, or incompatible response shapes keep Memory disabled. Probe prompts and
  responses are fixed synthetic values and are never logged.
- Keys are write-only in every response; omission preserves the current key and
  explicit clear removes it only while Memory is disabled or being cleared.
- Provider configuration files contain no keys. The controller passes keys only
  through the owned sidecar's child-process environment, using EverOS's
  `EVEROS_*__API_KEY` settings.
- URLs are bounded absolute `http` or `https` URLs. Plain HTTP is allowed only
  for numeric loopback addresses; non-loopback destinations require normally
  verified HTTPS.
- Userinfo, query, fragment, empty model, UI mask values, and oversized fields
  are rejected before save.
- While provider memory data exists, changing the embedding `base_url` or
  `model` is rejected with a closed error; the owner runs Clear all first.
  This prevents silently mixing incompatible vector spaces in one index.
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
├── clear_in_progress      INTEGER NOT NULL DEFAULT 0
├── principal_id           TEXT NOT NULL
├── scope_key              BLOB NOT NULL
├── provider_root_id       TEXT NOT NULL
├── last_provider_timestamp_ms INTEGER NOT NULL DEFAULT 0
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
├── source_message_digest  TEXT PRIMARY KEY
├── epoch                  INTEGER NOT NULL
├── session_id             TEXT NOT NULL
├── payload_text           TEXT
├── occurred_at_ms         INTEGER NOT NULL
├── provider_timestamp_ms  INTEGER NOT NULL
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
- `provider_timestamp_ms` is assigned once when a distinct digest is inserted
  and never changes. Retries after timeout or restart reuse it so EverOS sees the
  same message identity.
- `delivered` and `dead` clear payload immediately and retain a content-free
  tombstone. A dead row keeps only its closed error category and counters.
- Terminal tombstones may compact after 90 days or at a fixed 100,000-row cap.
  Replaying a source older than that window can enqueue it again; the product
  already promises only bounded local idempotency.
- Clear removes every queue row, including tombstones.

SQLite uses the repository's normal durable settings and transaction helpers.
The MVP does not change all Avibe or provider connections to `synchronous=FULL`,
add directory-fsync barriers, or restrict filesystems to claim a stronger
power-loss contract than the rest of Avibe.

`memory.enabled` in V2 config is the only desired enablement state. Runtime
health, `starting`, `ready`, `indexing`, `degraded`, and `error` are observed or
derived values, not persisted lifecycle states. The store persists only the
`clear_in_progress` recovery marker because Clear all must resume after a crash.

## 9. Capture call paths

Workbench and authorized private IM are entry adapters to one capture interface.
Neither agent backends nor individual platform adapters own Memory business
logic.

```text
POST ordinary Workbench message
    -> session lookup + direct-loopback Memory eligibility predicate
    -> normalize and commit user row
    -> recheck direct loopback, no forwarded metadata, and CSRF
         -> ineligible: do not invoke Memory
         -> eligible: bounded payload from committed server fields
              -> internal_client.memory_capture(...) over dispatch.sock
                   -> MemoryModule.capture(request)
    -> continue ordinary agent dispatch regardless of Memory receipt

private IM human-text event
    -> platform adapter classifies DM and runs centralized authorization
    -> shared command dispatcher intercepts /memory before ordinary callback
    -> MessageHandler claims (platform, native_message_id) for deduplication
    -> prepare normalized context and stable source conversation reference
    -> controller rechecks is_dm + bound + enabled + administrator, fail closed
         -> ineligible: do not invoke Memory
         -> eligible: bounded payload from normalized server-owned context
              -> MemoryModule.capture(request)
    -> continue ordinary agent dispatch regardless of Memory receipt
```

Workbench uses the committed local message id. Private IM uses a canonical
source-qualified id such as `im:<platform>:<native_message_id>`; the module
stores only a scope-keyed digest of that value as the queue key. Both payloads
contain a source-neutral
conversation reference, normalized raw user text before framework metadata, and
server receipt time. Browser, adapter payload, or command arguments cannot
supply the principal, role decision, epoch, provider ids, or capture flag.

When the Workbench queue merges several composer segments into one committed
message, capture uses that merged row: its id is the `source_message_id`, its
merged normalized text is the payload, and the earliest segment's server
receipt time is `occurred_at_ms`. Framework-added metadata headers never enter
the captured text.

Private IM captures only ordinary human text. Group/MPIM, unbound, disabled,
non-administrator, scheduled/harness, bot/self, rich, forwarded/shared, edited,
attachment-bearing, empty, and command events do not call `MemoryModule`. The
existing shared command dispatcher consumes `/memory` before `MessageHandler`,
so a recognized command cannot be mirrored, captured, or agent-dispatched.

The Workbench hook remains in the shared UI route and uses the existing internal
UDS because UI and controller are separate processes. The IM hook sits once in
the shared `MessageHandler` after native-message deduplication and stable session
resolution. Platform adapters only normalize DM/auth/message facts. No change is
made to Claude, Codex, OpenCode, or their request types.

Capture validation:

- Memory config must be enabled and no Clear all may be in progress;
- text is normalized to NFC with CRLF/CR converted to LF;
- blank input and Memory route operations are skipped;
- UTF-8 text must be at most 32 KiB;
- ids and session values must satisfy fixed nonblank byte caps;
- queue nonterminal rows must remain below 500;
- observed free disk must be at least 512 MiB.

`queue_plaintext_bytes` remains a status measurement, not a second admission
limit. With 500 rows and a 32 KiB row limit, a 64 MiB plaintext limit could never
be reached and would be a dead constraint.

A validation or capacity skip increments only `memory_meta.missed_count`. It
does not retain the rejected text or create attacker-controlled per-cause rows.

There is an accepted crash window between the ordinary message commit and the
separate queue insert. The MVP does not add a cross-module database transaction
or startup history scan to close it. Dogfood measurements determine whether the
window justifies a later shared transactional outbox.

## 10. Worker lifecycle

One controller-owned task drains due rows while `memory.enabled` is true, no
Clear all is in progress, and the sidecar plus processing endpoints are healthy.
Global health failures pause new claims instead of spending per-row retries.

### 10.1 Claim

After the global health gate passes, a short transaction claims one `pending`
row whose `next_retry_at` is due by changing it to `processing` and setting the
current boot/task lease. The provider call occurs outside the transaction.
`attempts` counts completed message-level failures, not claims or infrastructure
outages.

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

The provider adapter classifies failures before the worker changes the row. It
does not infer a class from arbitrary provider prose. Classification follows
this order:

1. A child exit, UDS transport failure, or failed sidecar health check is a
   system failure.
2. A stable public EverOS status/code mapping may be used only after the pinned
   POC records and tests that mapping.
3. Every other provider error is ambiguous, so Avibe uses its existing model
   endpoint probes to decide which case occurred. The controller runs one bounded,
   redacted, synthetic probe against the configured LLM endpoint and one
   against the embedding endpoint. Only one probe pair can run across the
   controller at a time, and failed probes use bounded backoff so one bad row
   cannot create a probe storm.

If either endpoint probe or the sidecar health check fails, the ambiguous error
is a system failure: return the row to `pending`, do not increase `attempts`,
pause new claims, and re-run the global health gate on its normal backoff. If
all probes pass while the original row still fails, classify that occurrence as
a message failure. After a system outage recovers, the same pending row is
retried. If that row always fails even though the system is healthy (a "poison
row"), its repeated failures consume the bounded message budget and eventually
unblock later rows.

After that classification:

- a sidecar exit, connection failure, model endpoint outage, rate limit, bad
  endpoint credential, or invalid configured model is a system failure; return
  the row to `pending` without increasing `attempts`, pause new claims, and show
  the configuration or availability error in status;
- a failure tied to that message while the runtime and endpoints are healthy is
  a message failure; increment `attempts` and retry at +30 seconds and +2 minutes;
- the third message failure, or a non-retryable message error, changes the row to
  `dead` and clears `payload_text` in the same transaction.

`last_error` contains only a closed category such as:

- `memory_sidecar_unavailable`;
- `memory_processing_failed`;
- `memory_provider_timeout`;
- `memory_provider_response_invalid`;
- `memory_queue_full`;
- `memory_low_disk_space`.

There is no automatic fourth message attempt, evidence reconciliation, subset replay,
repair fence, owner drain, or provider-private read. Re-enable may resume
`pending` rows but does not re-arm `dead` rows. Clear is the MVP recovery for
unwanted pending content; dead rows contain no message text.

## 11. Read path

### 11.1 Entry authority

The UI routes and Workbench `/memory` command use a new
`is_direct_loopback_memory_request()` predicate before calling a typed internal
client wrapper. The route invokes the controller only after that supported
browser path has passed these checks:

- the TCP peer is loopback;
- the effective request host is loopback;
- neither trusted nor untrusted forwarded/proxy metadata is present;
- the request origin is same-origin;
- a mutation has the existing CSRF cookie/header pair.

This predicate is intentionally narrower than the existing
`_is_local_request()`: it never accepts trusted public-origin forwarding,
Docker-loopback exceptions, setup-host/LAN requests, or a remote-access cookie.
Local Workbench currently has no separate authenticated-human session, so the
route does not claim one. Its authority is the explicit one-install/one-OS-account
MVP trust model plus direct-loopback and CSRF facts.

The CLI uses Avibe's existing controller socket rather than an HTTP/TCP listener.
The socket is created in the effective Avibe state directory with mode `0600`;
the client and server fail closed unless the path is a non-symlink Unix socket
owned by the current UID with that mode. This is same-OS-account local IPC, not
an authenticated-human owner protocol. The CLI cannot supply a principal,
provider identity, root, or lifecycle value and never opens the Memory store,
provider root, or EverOS socket directly.

Private IM uses a separate admission proof. The platform adapter must have
classified the event as a one-to-one DM and completed centralized authorization;
controller-owned code then freshly loads settings and requires the exact
`(platform, user_id)` to be bound, enabled, and administrator before it calls
Memory. Missing settings, unknown platform, missing identity, stale role, or any
lookup error denies without calling Memory. `is_dm` or a caller-supplied user id
alone is insufficient. All current enabled administrators are co-owners of the
same pool by product contract; ordinary member DMs are outside this MVP because
the single pool provides no member isolation.

For browser reads, commands, and capture, the UI process applies the direct-local
predicate and then calls a typed `vibe.internal_client` wrapper. For IM reads and
capture, the controller applies the administrator-DM predicate after existing
transport auth. These adapters are the authorization seam. The controller does
not mint a same-process token that restates the same checks, and no client JSON
can bypass the adapter to select another principal or pool.

`capture`, `search`, and `profile` require enabled Memory; `status` remains
available through all four read surfaces when disabled so an eligible co-owner
can inspect retained state. Only the confirmed UI can call `clear` while
disabled. Revoking, disabling, or unbinding an administrator blocks new IM reads
and capture immediately on the next eligibility check; it does not delete text
that is already queued or derived. Clear all remains the only MVP operation that
removes retained Memory.

### 11.2 Bounds

Every UI, Workbench-command, private-IM-command, and CLI read uses the same fixed
limits:

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

## 12. Managed runtime

Memory does not introduce a second runtime installer. `MemoryArtifactManager`
is a thin specialization of the repository's existing
`core.managed_runtime.ManagedRuntimeManager`; it supplies the Memory manifest,
runtime directory, executable/version probe, and post-extraction smoke test. It
inherits the shared `ensure()`, `status()`, and `resolve_binary()` behavior used
by other Avibe-managed artifacts.

Dependencies calls `status()` and `ensure()`. `MemoryModule` resolves the
verified executable before it starts EverOS. Process ownership is not part of
the artifact Interface: a private `EverOSProcess` implementation inside Memory
owns start, health, socket, and stop behavior. Module contract tests inject the
existing fake provider and do not expose another caller-facing runtime port;
focused integration tests exercise the real process implementation.

### 12.1 Paths

```text
<AVIBE_HOME>/runtime/memory/
├── current.json                        # shared manager's atomic active pointer
├── versions/
│   └── <version>/<platform>/<fingerprint>/ # immutable Python + locked packages
├── downloads/                          # verified/cacheable archives and manifests
└── install-<random>/                    # temporary extraction; never active

<AVIBE_HOME>/memory/
├── everos-root/                        # provider data; sentinel owned
├── .rt/everos.sock                     # runtime socket in mode 0600
└── generated/                          # non-secret generated config only
```

Runtime artifacts are never inside `everos-root`. Clear removes provider data,
not runtime code or `current.json`. Every path is derived from the effective
Avibe home through shared path helpers.

### 12.2 Install, status, and repair

The existing Dependencies aggregate adds one stable id, `memory-runtime`, and
the existing asynchronous dependency job runner handles install and repair.
The React page localizes its label as "Memory runtime" and describes EverOS only
as the current implementation. Transitive packages never become separate rows.

The Avibe release pins one runtime manifest entry per supported target. Each
entry contains the runtime id, provider version, exact embedded Python version,
lock id and SHA-256, target, archive name, size, SHA-256 digest,
provider-root format, and the
older provider-root formats it declares compatible. Installation:

1. uses the shared process/file install locks;
2. downloads the Avibe-pinned target artifact through the shared bounded
   dependency-network layer;
3. verifies the manifest, target, size, digest, binary digest, and safe archive
   paths with the shared manager;
4. extracts into its shared owner-only `install-<random>` staging directory;
5. runs the Memory-specific smoke test for embedded Python/CLI identity, exact
   lock, and required native imports;
6. if no provider root exists yet, proceeds without creating one; first
   enablement creates it. If the path exists, reads `provider_root_format` from
   its sentinel and checks it against the candidate's own format plus its
   declared compatible older formats. An existing path with a missing sentinel
   or undeclared format fails closed. An incompatible nonempty root leaves the
   previous runtime active and requires Clear all before activation. After Clear
   all, the candidate may initialize only the verified empty sentinel-owned root
   with its own format and artifact fingerprint while preserving
   `provider_root_id`;
7. moves the verified directory into `versions/` and atomically replaces
   `current.json` through the shared manager;
8. asks the controller over the existing internal socket to reconcile the live
   Memory runtime when Memory is enabled.

Changing the format of a verified empty root is part of runtime activation, not
ordinary download or staging. `MemoryArtifactManager` pauses worker claims and
stops the sidecar through the controller before replacing the active pointer;
claims resume only after controller reconciliation. It keeps the previous
sentinel value until activation completes. If active-pointer replacement returns
an ordinary failure after the candidate sentinel is written, it atomically
restores the previous sentinel and restarts the previous runtime before releasing
the lifecycle lock. If that rollback fails, Memory stays down and claims remain
paused for Repair. If the process crashes between those writes, startup may
rewrite a mismatched sentinel only when the root is still verified empty. A
mismatch on a nonempty root always fails closed.

The request route only starts and polls this background job; it never resolves
packages or blocks for the installation. A failed staging directory is removed
without changing `current.json`; cleanup follows the shared previous-version
retention policy. The MVP has no custom runtime path, provider selector,
arbitrary PyPI installer, or downgrade control.

Dependency status is distinct from Memory status. Dependency status reports
`ready`, `missing`, `unsupported`, or `error` for the artifact; the UI overlays
`installing` while a background job is active. `MemoryModule.status()` reports
capture, sidecar, queue, and provider health. Installing a runtime does not
enable Memory, create a principal/root, or start a persistent sidecar. An Enable
request returns `dependency_not_ready` until the owner has completed install or
repair and explicitly retries Enable; no pending enable intent is persisted.

Startup reconciliation is best effort and runs outside the startup critical
path. It does not download `memory-runtime` when Memory is disabled and no
artifact exists. When Memory is enabled, a missing or incompatible artifact
keeps only Memory unavailable and exposes the shared repair action; Avibe and
ordinary chat still start.

### 12.3 Private process lifecycle

- The managed artifact contains only a verified Python distribution and the
  locked EverOS dependencies. The Avibe package supplies the child-only
  launcher through the explicit child `PYTHONPATH`. Inside the child process,
  that launcher loads the pinned
  `everos.entrypoints.api.app:create_app` ASGI factory and starts uvicorn with
  `uds=<verified path>`. The parent Avibe process and `MemoryModule` do not
  import EverOS. This version-pinned entry-point load is the only allowed
  package-internal integration and is covered by artifact and sidecar tests.
- The launcher wraps the ASGI application with a small text-only request guard.
  It accepts only `GET /health` and the exact MVP shapes for
  `POST /api/v1/memory/add`, `/flush`, `/search`, and `/get`. The `/get` guard
  permits only the fixed user principal and `memory_type=profile|episode`; no
  `agent_id` or agent-memory kind is accepted. The guard rejects every other
  route plus file or multimodal fields before EverOS parsers run.
- Execute only the verified embedded Python and package lock selected by the
  POC; never use a system Python, user site packages, `PATH` package tools, or an
  unbounded upstream version.
- Release construction pins Python `3.12.12`, the exact
  `scripts/memory_poc/harness/uv.lock` digest, and the uv installer version.
  Every platform archive must be at most 1 GiB. The final archive is extracted
  into a clean temporary home, loads `create_app`, starts the production child
  launcher, and passes an owner-only UDS `/health` probe before manifest
  generation can admit it.
- Launch the provider as an owned child with an owner-only working home and a
  minimal allowlisted environment: proxy and TLS-override variables such as
  `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, and
  `REQUESTS_CA_BUNDLE` are not inherited, and Avibe's provider-facing HTTP
  clients disable environment trust so recorded egress matches configuration.
- Bind the provider only to a Unix-domain socket in an owner-only directory.
  A pre-bind preflight rejects a socket path that would overflow the platform
  `sun_path` limit (104 bytes on Darwin, ~108 on Linux, including the
  terminating NUL) with a closed error instead of an obscure bind failure.
- Guarantee socket file mode `0600` explicitly: the pinned uvicorn creates new
  UDS files with mode `0o666`, so the manager chmods the bound socket and
  verifies its owner and mode with `lstat` before reporting healthy.
- Never expose the socket through remote access or a TCP listener.
- Disable body access logs and do not persist raw sidecar stdout/stderr.
- Generate and validate the provider configuration before capture opens. The
  generated config pins chat/user-memory mode, disables episode reflection and
  LLM rerank, restricts any file-URI ingestion directory setting to an empty
  Avibe-owned directory, sets the fixed project mapping, and records the local
  IANA timezone resolved at first enablement. Avibe validates this generated
  structure before launch and the launcher receives only that file plus an
  allowlisted environment; it does not parse masked `everos config show` output.
- The timezone is fixed for the current provider root. Moving the computer to a
  different timezone does not rebucket existing episodes. Clear all creates a
  new root and resolves the timezone again on the next enablement.
- Generated files contain endpoints and non-secret fixed settings only. Pass API
  keys exclusively through the owned child-process environment using EverOS's
  documented `EVEROS_*__API_KEY` variables.
- Treat the configured processing endpoints as trusted destinations for the
  experimental MVP. The POC records redirects and egress; a stricter relay or
  fork is a later decision if evidence requires it.
- Stop only the PID started by `EverOSProcess`. Never adopt or kill an unknown
  process found at a path.

`EverOSProcess` watches the child exit and removes only its owned stale socket.
Unexpected exits restart after 1, 5, 30, and 120 seconds. After five consecutive
failed starts, automatic restart stops, Memory remains `down`, queued text stays
pending, and the UI offers Repair or an explicit Enable retry. Five healthy
minutes reset the crash counter. Enable, Disable, Clear all, runtime
activation/reconciliation, and automatic restart share the lifecycle lock so
only one child can start.

The UDS removes the upstream wildcard-CORS browser network path. Same-user local
code can still open the socket or read files and is inside the stated desktop
trust model.

## 13. Clear lifecycle

The module owns one async lifecycle lock shared by enable, disable, clear,
runtime activation/reconciliation, and sidecar restart.

Clear sequence:

1. acquire the lifecycle lock;
2. persist `clear_in_progress=1`, increment `epoch`, and reset
   `missed_count`, `last_success_at`, and `last_error` in one transaction;
3. stop new worker claims and wait a bounded time for the current task;
4. stop and prove exit of the owned sidecar;
5. verify the exact expected root path, owner, type, and root-id sentinel;
6. remove root children with no-follow traversal;
7. delete every queue row and recreate the empty sentinel-owned root with the
   persisted `provider_root_id` plus the active artifact's root format and
   fingerprint;
8. persist `clear_in_progress=0` after local deletion is complete;
9. if config remains enabled, start and health-check the sidecar. A restart
   failure is reflected by observed status while the completed clear stays final.

Startup checks `clear_in_progress` before starting any worker or sidecar and
resumes at step 3. All remaining steps are idempotent. The operation needs no
durable action receipt, broader lifecycle state, recovery matrix, backup
discovery, or export cut.

The UI confirmation is fresh interaction plus the ordinary CSRF proof. It is
not persisted across restart; an exact retry simply performs the same idempotent
clear again.

## 14. Entry adapters

### 14.1 Workbench HTTP and `/memory`

All browser routes require the Memory-specific direct-loopback predicate and use
native async FastAPI handlers. The UI server is a separate process: after
authentication, normalization, and response shaping, each handler awaits a
typed `vibe.internal_client` wrapper over `dispatch.sock`. It never imports the
controller-owned `MemoryModule`, store, provider port, or `EverOSProcess`.

The controller-side native async FastAPI handler awaits `MemoryModule` on the
controller loop after the UI route has authorized the request. Blocking SQLite
and filesystem work inside the module uses the repository's bounded threadpool
convention; neither process introduces per-request `asyncio.run()` bridges.

```text
GET   /api/memory/settings
PATCH /api/memory/settings
GET   /api/memory/status
GET   /api/memory/profile
POST  /api/memory/search
POST  /api/memory/clear
POST  /api/sessions/<session_id>/messages  # existing route; /memory intercept
```

Rules:

- Every response containing Memory content uses `Cache-Control: no-store`.
- Browser search/profile/status/clear and Workbench `/memory` never create an
  ordinary message or agent turn.
- The search body contains only bounded query and limit fields.
- Clear requires an explicit confirmation boolean/string accepted only after the
  UI modal and CSRF checks.
- Settings responses expose `has_api_key`, never the key or reusable mask.
- No endpoint accepts principal, provider project, provider root, session ref,
  local path, scope key, epoch, or socket path from client JSON.

The UI uses text nodes for provider content. It does not use raw HTML rendering
or pass results through generic Markdown/directive processing.

The message route recognizes a Memory command only when the payload is plain
text, has no attachments or quick-reply metadata, and matches this complete
grammar after ordinary Unicode/newline normalization:

```text
/memory
/memory help
/memory status
/memory profile
/memory search <nonblank bounded query>
```

It performs the intercept after session lookup and the direct-loopback/CSRF
checks but before attachment resolution, `_persist_user_row()`, capture, or dispatch. The
response contains a typed `memory_command_result` that the composer renders in
an ephemeral inert-text panel. Invalid subcommands return bounded help/error
data through the same response shape. The parser does not accept clear,
configuration, capture, export, deletion, or provider identifiers.

`ChatPage.sendMessage()` handles `memory_command_result` before the ordinary
`body.id`/queued branches: it stores only ephemeral component state, does not
append a `WorkbenchMessage`, and reconciles the authoritative turn state instead
of blindly clearing `working`. A command issued while another turn is running
therefore leaves that turn's Stop/working indicator intact.

### 14.2 Private IM `/memory`

The controller registers `memory` once in the shared command map used by Slack,
Discord, Telegram, Lark/Feishu, and WeChat. Each adapter performs its normal DM
classification and centralized authorization before command dispatch. The
handler then applies the fail-closed administrator co-owner check from section
11.1; it does not trust an adapter-supplied eligibility flag.

The private-IM grammar is identical to Workbench:

```text
/memory
/memory help
/memory status
/memory profile
/memory search <nonblank bounded query>
```

The adapter intercepts a recognized command before the ordinary message
callback. The command therefore creates no inbound Avibe message, capture,
agent turn, SSE, or inbox/search event. When the platform supplies a stable
command/event id, the handler claims its platform-qualified form before provider
access. A platform retry without such an id may repeat the non-mutating read and
bot reply; this is a bounded MVP limitation, never a Memory write.

An eligible result returns through the existing platform `send_message` path as
bounded inert plain text. The response path disables active mentions, Markdown
actions, link previews, file/directive enhancement, quick replies, and buttons;
it truncates or splits only at documented platform-safe bounds. The resulting
bot message may be retained in the IM platform's chat history and device sync.
An ineligible request receives one generic unavailable response and cannot
distinguish disabled Memory from failed owner eligibility.

Slack native `/memory` must be declared in the Slack App configuration so Slack
delivers its slash-command payload. Other platforms must verify the literal
text-command UX in their contract tests; adding a native application-command
registration is an adapter concern and does not change `MemoryModule`.

### 14.3 Local CLI over the controller UDS

The CLI adds only:

```text
vibe memory status [--json]
vibe memory profile [--json]
vibe memory search <query> [--limit 1..20] [--json]
```

All non-settings Memory operations use new handlers on the already-running
controller's existing `dispatch.sock`:

```text
POST /internal/memory/capture  # Workbench UI process -> controller only
GET  /internal/memory/status   # UI, Workbench command, or CLI
GET  /internal/memory/profile  # UI, Workbench command, or CLI
POST /internal/memory/search   # UI, Workbench command, or CLI
POST /internal/memory/clear    # exposed only by confirmed UI in product
```

Settings persistence remains in the UI/config service and uses the existing live
controller reconciliation pattern. The internal Memory handlers do not accept a
client-supplied principal, root, endpoint, or lifecycle state; after their entry
checks they await the same controller-owned `MemoryModule` methods.

`capture` and `clear` need internal handlers only because the UI and controller
are separate processes. Private-IM capture and reads are already in the
controller process and call the same module directly after authorization; they
do not loop through the UDS. Capture and clear are not registered as CLI
subcommands. Same-account code is already inside the stated local trust boundary,
but the supported write surfaces remain automatic Workbench/private-IM capture
and confirmed UI-only clear.

The CLI exposes only status/profile/search through typed `vibe.internal_client`
wrappers. It is a presentation adapter: it does not start a standalone Memory
runtime, read SQLite/Markdown, call EverOS, add retries, or acquire a Memory
lifecycle lock. When Avibe is not running it returns a closed service unavailable
error. `--json` is versioned and stable for local automation; human output may
improve without becoming a second API.

## 15. Status semantics

Precedence:

1. `clear_in_progress=1` -> `clearing`;
2. config disabled -> `disabled`;
3. required runtime missing, incompatible, unsupported, or broken -> `error` with
   the matching `memory_runtime_*` code;
4. an in-process enable/reconcile operation is starting the sidecar -> `starting`;
5. sidecar unexpectedly unreachable -> `down`;
6. a processing endpoint/configuration pause, any dead work, capacity/disk
   pause, or last message-processing failure -> `degraded`;
7. pending or processing work -> `indexing`;
8. reachable sidecar -> `ready`.

Authorization failures are returned by entry adapters and do not become a
`MemoryStatus` state. No runtime state other than `clear_in_progress` and queue
facts is persisted merely to render this precedence.

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

- disabled behavior and clear-in-progress exclusion;
- capture validation and duplicate source id;
- queue row/input/disk caps and plaintext-byte status measurement;
- worker success, message-level bounded retry, dead state, and payload scrubbing;
- explicit sidecar/endpoint outages pause claims and do not increase row attempts;
- an ambiguous provider failure plus a failed sidecar or endpoint probe returns
  the row to pending without increasing attempts, while the same failure with
  all probes healthy consumes one message attempt;
- endpoint recovery followed by a permanently failing row reaches `dead`
  after the bounded message budget and allows the next due row to proceed;
- endpoint probes are bounded, redacted, and synthetic; only one pair runs
  across the controller at a time, with backoff while the global pause remains
  active;
- provider timestamps are allocated once per distinct digest and reused across
  timeout, retry, and restart;
- old-boot `processing` reclaim and documented duplicate possibility;
- search/profile bounds and closed errors;
- idempotent clear and interrupted-clear startup recovery;
- status precedence;
- no raw content in logs/errors.

### 17.2 Managed runtime and Dependencies tests

- status maps missing, ready, unsupported, and error artifacts without starting
  a sidecar;
- target, archive-path, size, digest, embedded Python, exact-lock, CLI-version,
  and native-import failures never replace the last verified artifact;
- install jobs deduplicate concurrent requests, report bounded progress/errors,
  and never run package installation on the HTTP request path;
- a clean supported host installs from the pinned artifact without system Python
  3.12, user site packages, or model credentials;
- a fresh install with no provider root activates successfully, then first
  enablement creates the root and complete sentinel; a pre-existing path without
  a valid sentinel fails closed;
- disabled + never-installed does not auto-download, while enabled + missing
  leaves only Memory unavailable and exposes repair;
- successful activation reconciles the controller once when Memory is enabled; a
  failed reconcile is visible and startup resolves from `current.json`;
- Clear all and provider-root recovery never remove runtime artifacts;
- root creation and Clear all write a sentinel whose root id, provider id,
  format, and artifact fingerprint are checked before the provider starts;
- same-format repair and a declared compatible older format preserve the
  provider root; an incompatible format keeps the previous artifact active until
  Clear all, after which only the verified empty sentinel-owned root can be
  initialized with the candidate format;
- interrupted activation may reconcile a format mismatch only for a verified
  empty root; the same mismatch on a nonempty root fails closed;
- an active-pointer write failure restores the previous sentinel and runtime
  before claims resume; a failed rollback leaves Memory down and fail-closed.

### 17.3 Store tests

- schema/check constraints and indexes;
- atomic claim and concurrent duplicate insert;
- terminal payload clearing and retention compaction;
- atomic global provider-timestamp allocation, duplicate non-advancement, and
  retry reuse;
- epoch change and queue deletion during clear;
- effective-home isolation so tests never write real user state.

### 17.4 Workbench route and command tests

- direct loopback + same origin required, with CSRF on every mutation, without
  claiming a separate local human login;
- trusted/untrusted forwarded metadata, trusted public-origin, Docker-loopback,
  setup-host/LAN, remote-cookie, and other network paths denied;
- an Avibe Cloud request sees only a static local-device-required page and does
  not fetch or reveal Memory state;
- keys remain write-only;
- generated provider files contain no keys and child environment canaries never
  appear in status, logs, or serialized errors;
- a generic non-Memory config save round-trip preserves the memory block and
  its stored keys;
- no-store content responses;
- exact `/memory` grammar is intercepted before attachment resolution, message
  persistence, capture, or agent dispatch;
- command completion while idle or while another turn is running restores the
  correct authoritative working/Stop state and never enters the queued prompt
  path;
- search/profile/clear and command results create no message, SSE, inbox, or
  agent event;
- provider content renders as inert text;
- browser routes and capture use mocked typed internal-client wrappers and never
  import or instantiate `MemoryModule`, its store, provider, artifact manager, or
  process implementation in the UI process;
- accepted config reconcile updates the live controller once.

### 17.5 Private IM contract tests

- Slack, Discord, Telegram, Lark/Feishu, and WeChat normalize their supported
  one-to-one private-message marker and preserve a platform-qualified native id;
- bound + enabled + administrator DMs can capture and read; unbound, disabled,
  non-administrator, group/MPIM, scheduled/harness, bot/self, rich, forwarded,
  edited, and attachment-bearing inputs produce zero queue/provider operations;
- missing settings, platform, user, native message id, or role lookup fails
  closed without revealing whether Memory is enabled;
- administrator promotion, revocation, disablement, and unbinding are reflected
  on the next command/capture check;
- two administrator identities intentionally write and read one shared pool;
- dogfood reports contradictory current-profile items attributable to the shared
  administrator pool as a named quality metric;
- `/memory` interception and any available platform-qualified command replay
  claim occur before mirror, persistence, capture, or agent dispatch; a platform
  without a stable command id can only repeat the bounded non-mutating reply;
- duplicate `(platform, native_message_id)` captures one row while equal native
  ids from different platforms create distinct digests;
- source-message and source-session digests contain no raw platform, native
  message, user, chat, or thread id;
- private-IM output is bounded inert text with no mentions, links, Markdown
  actions, quick replies, files, or directives, and only the explicit platform
  response is emitted;
- provider failure never delays or changes the ordinary IM agent turn;
- Slack native command delivery and every other platform's literal/native
  command UX are covered by adapter contracts.

### 17.6 CLI adapter tests

- CLI uses the verified mode-`0600` same-account controller UDS and never reads the Memory
  store, provider root, or sidecar directly;
- internal capture/status/profile/search/clear handlers authorize their surface
  and invoke the controller-owned module on the controller event loop;
- `status`, `profile`, and `search` human output and versioned `--json` output;
- service-down, disabled, timeout, malformed response, and bound errors retain
  closed codes and useful exit status.

### 17.7 EverOS integration tests

The phase-0 POC supplies the provider facts. Production integration tests reuse
its synthetic fixtures for:

- pinned managed artifact/config startup;
- launcher-owned ASGI factory load, exact route/shape text-only request guard,
  and UDS-only health;
- add+flush through `EverOSPort.ingest()`;
- search/profile mapping and response bounds;
- stop/restart;
- root sentinel and full clear;
- child environment and destination recording;
- authenticated enablement probes and the probes used to classify ambiguous
  failures for both model endpoints.

Production tests do not copy a provider recovery algorithm into a second
harness.

### 17.8 User-facing verification

After implementation:

- run focused pytest files first;
- run `ruff check` on changed Python files;
- run the UI build;
- run the relevant scenario test;
- verify Dependencies -> install/repair -> Memory enable on a clean supported
  Incus host with no system Python 3.12;
- use the local Incus regression workflow for the Workbench enable -> capture ->
  UI/command/CLI search/profile -> clear path plus at least one authorized
  private-IM capture and `/memory` round trip on each configured platform;
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
- controller wiring and clear-recovery marker.

This slice can be tested without EverOS or model credentials.

### Slice 2: local provider and settings

- managed `memory-runtime` manifest, installer, status, and repair path;
- `MemoryArtifactManager` specialization and private `EverOSProcess` lifecycle;
- selected provider port;
- V2 config and live reconciliation;
- loopback-only routes;
- disclosure, status, and clear.

### Slice 3: Workbench, private IM, and CLI vertical path

- committed Workbench capture hook and shared private-IM admission/capture seam;
- profile/search/status UI;
- exact Workbench `/memory` intercept and ephemeral result panel;
- shared private-IM `/memory` command, safe response path, replay claim, Slack
  command registration, and five-platform contract tests;
- local `vibe memory` commands over the controller UDS;
- end-to-end and Incus verification for the direct read paths;
- experimental feature flag.

### Slice 4: experimental validation

- synthetic and opt-in dogfood evaluation;
- queue/provider and explicit-read usefulness measurements;
- decision to harden official EverOS, maintain a small fork, or stop.

No slice adds a group/non-administrator IM surface, registered agent-facing
Memory tool, write-capable command/CLI operation, or automatic recall.

## 19. Evidence-dependent questions

Only these questions may change the implementation candidate. Questions 1-6
belong to the provider POC:

1. Does official EverOS satisfy quality and resource gates?
2. Is add+flush per capture acceptable in latency and model cost?
3. Can public provider responses safely implement search/profile mapping?
4. Is at-least-once duplicate behavior acceptable for experimental use?
5. Which tested OS/runtime combinations can ship initially?
6. Does the integration need a small fork for stable write, auth, or egress
   behavior?
7. Can the exact dependency closure be packaged as a verified, relocatable
   Avibe-managed artifact for each initial target without a system Python 3.12?

If an answer requires the old `WriteEvidence`, per-message recovery, backend
taint, remote-owner registry, or cross-process Memory-specific config protocol,
the design does not silently restore it. The provider or MVP scope is revisited.

## 20. Superseded design elements

The following rev37 elements are intentionally removed from the active design:

- provider-neutral future capability surface;
- same-process `OwnerGrant` restatement of entry-adapter authorization;
- registered agent-facing Memory tools, MCP bridges, OS-enforced turn-scoped
  read grants, and backend-specific tool registration; eligible interactive
  turns may receive guidance for the existing same-account read-only CLI;
- `workspace_id` and Plan-B switch;
- `forget`, `remember`, automatic `recall`, and export;
- assistant capture and backend native-context taint;
- dispatch-id propagation across all agent backends;
- group, non-administrator DM, Cloud/network authorization, per-user pools, and
  remote-owner tables;
- adapter-specific Memory business logic or direct IM bypasses around the shared
  command/admission seams;
- provider `WriteEvidence`, affected-source sets, per-message dispositions,
  repair fences, and `awaiting_flush` lifecycle;
- separate outbox/source/operation/export/flush/clock/snapshot/command/action
  tables;
- strong fsync/directory-barrier filesystem contract;
- the broad rev37 text-only ASGI protection subsystem; the MVP retains only the
  small allowlist guard in the versioned runtime launcher because Avibe is the
  sole supported client;
- controller-owned provider clock tables; the MVP keeps one global counter in
  `memory_meta` and one immutable timestamp on each queue row;
- migration-backup deletion;
- processing transition receipts and custom egress relay;
- foresight Markdown reader and lineage verification;
- 36-round changelog inside the normative document.

Git history remains the record for those investigations. Useful findings can be
reintroduced only when a later scoped capability actually needs them.
