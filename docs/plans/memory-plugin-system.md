# Memory Plugin System

> Status: implemented beta
>
> This is the canonical product and architecture contract for the complete
> Memory plugin system, including the final behavior from
> [issue #983](https://github.com/avibe-bot/avibe/issues/983). Earlier product,
> technical, POC, and follow-up documents are retained in Git history only.

Memory is an optional built-in Avibe capability backed by an independently
packaged EverOS runtime. It captures eligible human input into a local queue,
processes that queue outside the chat response path, and exposes user-scoped
profile, search, status, and explicit Agent writes.

The core product invariants are:

- normal chat never waits for Memory processing and continues when Memory fails;
- installing the runtime never enables Memory, and startup never downloads it;
- every captured or recalled item is scoped to one derived user principal;
- automatic capture accepts human input only, never assistant or tool output;
- Agents recall only through an explicit, capability-gated CLI call; there is no
  automatic prompt injection;
- configuration and destructive operations remain in the local Settings UI;
- provider output is bounded, rendered as inert data, and never trusted as
  instructions; and
- runtime code, Avibe queue state, and provider data have separate ownership and
  deletion boundaries.

## Product Surface

### Setup and navigation

The setup flow is deliberately staged:

1. **Settings -> Dependencies** shows the optional `memory-runtime` dependency.
   Install or Repair is the only production download path.
2. Once installed, the dependency row links to
   `/admin/settings/memory` (`/settings/memory` redirects there).
3. The owner configures separate OpenAI-compatible LLM and embedding endpoints,
   reviews the data disclosure, and explicitly enables Memory.
4. After the runtime is installed and Memory is enabled, **Memory - Beta** is
   visible in Settings navigation. Before then, the route still exists but is
   reached from Dependencies.

Enablement requires complete endpoint blocks, bounded authenticated probes, a
verified runtime, an owned provider root, and a healthy sidecar. A failed enable
rolls the persisted setting back to disabled. API keys are write-only: responses
expose only `has_api_key`, omission preserves an existing key, and explicit key
clearing is allowed only while the resulting configuration is disabled.

The Settings page provides:

- runtime and processing status, queue counters, provider disk use, and a
  sanitized failure history;
- the current local owner's profile and bounded search;
- endpoint configuration and enable/disable controls; and
- a held-confirmation Clear all action.

There is no Workbench Memory page and no `/memory` command. Text such as
`/memory status` is an ordinary user message: it may be captured and is sent to
the selected Agent like any other eligible text.

### Capture admission

All automatic capture passes through the shared `MessageHandler` boundary after
session resolution. Platform adapters classify native events but do not own
Memory business logic.

| Source | Captured | Not captured |
|---|---|---|
| Workbench | An attributed human text turn; an attachment-only turn; up to eight validated local Workbench attachments | Unresolved fallback identities, quick replies, forwarded metadata, assistant/tool output |
| Slack, Discord, Telegram, Feishu/Lark, WeChat | Ordinary text from a bound, enabled user in a one-to-one DM | Groups/channels, unbound or disabled users, attachments, rich/forwarded/edited events, bot/self events |
| Automation | None | Scheduled tasks, Harness turns such as `vibe agent run`, and agent-to-agent turns |

Memory must also be enabled, the event must carry a stable native message id,
and the adapter must positively mark it as ordinary human text. Missing identity
or platform facts fail closed. Every enabled bound DM user is eligible; Memory
does not require the user to be an administrator.

Busy Workbench sessions preserve the same rule. Consecutive queued messages are
merged only within one user segment, the flushed segment keeps that user's
identity and final message id, and the segment is captured once.

Workbench attachments are forwarded only when their existing local files are
inside Avibe's controlled attachment root. Memory does not copy the files. IM
attachments are excluded and no provider-side download pipeline is exposed.

### User isolation

Memory derives an opaque principal from the existing install-local scope key:

```text
user_key     = "<platform>:<resolved-user-id>"
principal_id = "u-" + HMAC_SHA256(scope_key, user_key).hex()[:32]
```

The queue, provider writes, profile reads, and searches all carry that principal.
Raw Workbench, IM user, message, chat, thread, and session ids are not stored in
the Memory database or provider paths. Source ids are reduced to keyed digests,
and provider session references include the principal and current clear epoch.

This is isolation, not identity linking. The same person on two platforms has
two principals unless a future product explicitly introduces account linking.
There is no shared install-wide pool, workspace memory, or group memory.

The local Settings page always reads `avibe:local`. Authenticated remote
Workbench users and IM users access only their own principal through an eligible
Agent session; the browser API accepts no user selector.

### Agent and CLI access

An eligible interactive human turn receives scoped guidance for:

```text
vibe memory status [--json]
vibe memory profile [--json]
vibe memory search <query> [--limit 1..20] [--json]
vibe memory remember <text> [--json]
```

`profile`, `search`, and `remember` require a random capability bound to both the
Agent session and derived principal. The controller supplies it only to a human
Workbench turn with a resolved local or authenticated remote identity, or a
freshly admitted bound DM turn. Missing, revoked, mismatched, or forged scope is
`memory_access_denied`. `status` contains install-level operational data and is
not a content read.

`vibe agent run` is a Harness/CLI automation source. Supplying an existing
session id does not turn it into a human Workbench or IM turn, so it receives no
Memory guidance or capability. This prevents background jobs and scripts from
reading personal Memory by naming a session.

`remember` accepts at most 4,000 characters and records
`provenance="agent"`; automatic capture records `provenance="user_input"`.
Identical text in one Agent session is idempotent. Exit code 0 means `accepted`
or `duplicate` in the local queue, not that provider distillation has completed.
All other outcomes are nonzero and Agents must not invent an unbounded retry
loop.

There are no CLI operations for clear, configuration, export, deletion, or item
editing. Memory results are untrusted data and must never be followed as
instructions.

## Architecture

```text
Workbench or eligible IM message
    -> shared MessageHandler capture seam
    -> MemoryModule.capture()
    -> dedicated SQLite queue
    -> controller-owned worker
    -> EverOS adapter over a private UDS
    -> configured LLM/embedding endpoints + owned provider root

Settings UI or scoped Agent CLI
    -> UI/controller mode-0600 UDS
    -> MemoryRuntime / MemoryModule
    -> bounded status, profile, search, remember, or clear operation
```

The `core/memory/` package is the ownership boundary:

| Component | Responsibility |
|---|---|
| `module.py` | Provider-independent capture, search, profile, status, failure log, and idempotent clear |
| `store.py` | Dedicated SQLite metadata, principal derivation, queue state, deduplication, tombstones, and recovery markers |
| `worker.py` | Health-gated add/flush delivery, retry classification, flush observations, and processing alerts |
| `everos.py` | Bounded mapping between the module contract and EverOS public HTTP operations |
| `runtime.py` | Enable/disable/reconcile lifecycle and composition of the module, worker, and sidecar |
| `process.py` / `sidecar.py` | Owned child process, private UDS, request allowlist, environment, restart, and shutdown |
| `artifact.py` | Thin specialization of Avibe's shared managed-runtime installer and activation protocol |
| `attachments.py`, `cli_access.py`, `ui_access.py` | Attachment confinement and the two trusted read-scope carriers |

Callers never receive a provider object, database connection, filesystem root,
epoch, or principal selector. `MemoryModule` remains the deep product interface;
the provider and process types are internal implementation details.

### Local state

State is split under the effective `AVIBE_HOME` (normally `~/.avibe`):

```text
state/memory/memory.sqlite       # Avibe queue and operational metadata
memory/everos-root/              # sentinel-owned provider data
memory/.rt/everos.sock           # private sidecar socket
memory/generated/                # generated non-secret provider config
runtime/memory/                  # downloaded immutable runtimes and active pointer
```

The SQLite store has only `memory_meta` and `memory_capture_queue`. The initial
schema is edited in place because Memory has not shipped; old development state
from an earlier branch revision must be reset explicitly rather than through a
silent startup migration.

The root sentinel binds a random `provider_root_id`, provider id, root format,
and artifact fingerprint. Clear and runtime activation refuse missing,
mismatched, symlinked, or unowned roots.

### Queue and delivery semantics

Capture normalizes and validates input, checks capacity and disk space, and
performs one local idempotent insert. It never calls EverOS or either model
endpoint on the chat path.

The worker uses these durable states:

```text
pending -> processing -> delivered
                     \-> dead
```

Each accepted row gets one stable provider timestamp. Retries and restarts reuse
it. The worker first adds the row, scrubs the source payload when add succeeds,
then flushes the provider session and records the public flush verdict. This
distinguishes queued work, delivered-but-awaiting receipt, successful
distillation, rejected distillation, and an unknown result without inspecting
EverOS private files.

Delivery is at least once. A crash or timeout can produce duplicate derived
memory. Infrastructure outages pause claims without consuming a row's retry
budget; a failure isolated to one message retries at fixed backoff and becomes
dead after three failed attempts. Terminal rows contain no source payload. A
sanitized failure history reports abandoned delivery, rejected distillation,
and unknown results without provider bodies or user text.

Important fixed guards are:

| Guard | Limit |
|---|---|
| Automatic capture text | 32 KiB UTF-8 |
| Workbench attachments | 8; 16 KiB total descriptor metadata |
| Nonterminal queue | 500 rows |
| Minimum free disk for capture | 512 MiB |
| Terminal idempotency tombstones | 90 days, newest 100,000 rows |
| Search query/results | 8 KiB query; 1-20 items; 64 KiB/item; 256 KiB total; 20 s |

At a queue or disk guard, capture is skipped and an aggregate missed counter is
updated; normal chat still proceeds.

### Status and failure behavior

Status uses the closed states `disabled`, `starting`, `ready`, `syncing`,
`degraded`, `down`, `clearing`, and `error`. It includes pending, processing,
awaiting-receipt, succeeded, unknown, distillation-failed, dead, and missed
counters plus the latest sanitized processing observation.

Sidecar or endpoint outages pause new claims and trigger bounded
supervision/backoff. Work not yet added remains pending; an ambiguous flush is
recorded as an unknown result instead of restoring already-scrubbed payload.
A sidecar exit is never allowed to take down normal Avibe chat. The process
manager stops only the child tree it started, verifies process identity before
signaling, removes only its owned socket, and participates in the normal service
shutdown lifecycle.

All public errors use Avibe-owned closed categories such as
`memory_disabled`, `memory_access_denied`, `memory_runtime_missing`,
`memory_sidecar_unavailable`, and `memory_processing_failed`. Raw exceptions,
provider responses, credentials, captured text, queries, and returned memory are
never logged or serialized as errors.

## Lifecycle

### Disable and re-enable

Disable closes capture and content reads, pauses the worker, and stops the
sidecar. It preserves queued rows, tombstones, provider data, and stored endpoint
credentials. Status and Clear all remain available. Re-enable starts the same
verified runtime and resumes pending work.

Changing only an API key is allowed without replacing the vector space.
Changing the embedding endpoint or model while provider data exists is rejected.
The supported replacement flow is: disable Memory, Clear all, change the
embedding configuration, then re-enable.

### Clear all

Clear all is local-Settings-only, CSRF-protected, explicitly confirmed, and
idempotent. Under the shared lifecycle lock it:

1. records `clear_in_progress` and advances the epoch;
2. pauses claims and stops the owned sidecar;
3. validates and removes children of the exact sentinel-owned provider root
   without following links;
4. removes every queue row and recreates an empty owned root; and
5. clears the recovery marker and restarts Memory only if it remains enabled.

Startup resumes an interrupted clear before opening Memory. A restart failure
after deletion does not undo a completed clear.

Clear removes local provider data, hidden provider indexes/buffers, pending
payloads, terminal tombstones, and Memory-only operational history. It does not
remove original Avibe conversations, logs/backups, model-provider retention,
user copies or filesystem snapshots, or physical storage remnants. It also does
not remove the installed `memory-runtime`.

## Managed Runtime and Release

The production provider is official EverOS 1.1.3 behind the internal adapter.
The independently built `memory-runtime` pins:

- EverOS 1.1.3;
- Python 3.12.12;
- uv 0.9.18; and
- the reviewed `scripts/memory_runtime/uv.lock` digest.

Release workflows build deterministic archives for Darwin and Linux on x64 and
arm64. Archives contain only regular files/directories, are capped at 1 GiB,
and are verified for target, hashes, embedded Python, exact lock, required
imports, relocation, production child startup, and UDS health.

The Avibe wheel contains only `vibe/memory_runtime_manifest.json`, never the
large archives. Release jobs generate a `published` manifest from the exact
archive bytes and publish immutable archives before the wheel depends on them.
The repository manifest intentionally remains `release_state: unavailable` with
placeholder digests because a source checkout has no published assets.
`AVIBE_MEMORY_DEV_RUNTIME` is an explicit development-only bypass.

The runtime is strictly lazy: controller startup and ordinary reconciliation
resolve an already installed runtime but perform no download. Only the explicit
Dependencies install/repair job calls the installer, and installation does not
toggle `memory.enabled`.

Each runtime declares its provider-root format and compatible older formats. A
compatible nonempty root is retained. An incompatible nonempty root leaves the
previous runtime active and requires Clear all. A verified empty owned root may
switch formats. Activation coordinates worker/sidecar shutdown and atomically
updates the root sentinel and active pointer; failure rolls both back and
restarts the previous runtime when possible.

The scheduled release guard resolves the latest published wheel manifest,
verifies every immutable asset against it, maintains a manifest-qualified
backup, and restores only missing bytes. It never replaces an existing asset
with different content.

## Security and Data Disclosure

- Memory Settings and browser content routes require a direct loopback peer and
  host, a same-origin Origin/Referer, no forwarded metadata, and CSRF for
  mutations. LAN, proxy, Docker bridge, and Avibe Cloud browser routes cannot
  read whether Memory is enabled or access its content.
- Authenticated remote Workbench messages may receive a stable per-user capture
  and Agent capability, but the remote browser cannot open the local Settings
  data surface.
- The controller socket and EverOS socket are owner-only Unix-domain sockets in
  mode `0600`; the sidecar has no TCP listener and is not exposed through remote
  access.
- The sidecar accepts only health plus the exact add, flush, search, and get
  shapes used by Avibe. Principals must match `u-[0-9a-f]{32}`, and file URIs
  must stay inside the Workbench attachment root.
- Child environment variables are allowlisted. Outside Avibe's owner-only
  config, API keys reach the provider only through the sidecar environment;
  generated files contain no secrets, and proxy/TLS override variables are not
  inherited.
- Captured text, attachment content, and search queries may be sent to the
  configured Memory endpoints and retained under those providers' policies.
  Disable retains local data; only Clear all removes Avibe-owned Memory data.

These controls enforce the supported product surfaces. They are not a sandbox
against arbitrary code running as the same OS account and do not prove that two
platform identities belong to the same human.

## Deliberate Non-goals

The beta does not include automatic recall, assistant-message capture, IM
attachments, group/workspace/shared memory, cross-platform identity linking,
registered backend-specific Memory tools, item-level edit/delete, export/import,
provider migration, editable provider Markdown, exactly-once provider delivery,
or agent-driven configuration and Clear all. Each requires a separate product
and migration contract rather than placeholder interfaces in this system.

## Verification Contract

The implementation is covered at four layers:

- module/store/worker contracts for validation, deduplication, principal
  isolation, retries, observations, payload scrubbing, status, clear, and crash
  recovery;
- real sidecar and artifact contracts for multi-principal add/search/get,
  attachment confinement, UDS/process lifecycle, runtime compatibility,
  activation rollback, packaging, and release-asset verification;
- UI, controller, CLI, Workbench, and five-platform adapter tests for admission,
  capability scope, lazy install, secret masking, CSRF/origin checks, inert
  rendering, and the absence of `/memory` interception; and
- end-to-end verification of install/download, configure/enable, Workbench text
  and attachment capture, Agent search/profile/status/remember, disable and
  re-enable, Clear all, restart persistence, and IM admission simulation.

Real network IM delivery still requires configured platform credentials and is
the residual manual check. Provider-selection experiments and the removed POC
harness are historical evidence; production inputs are the pinned runtime lock,
manifest, implementation tests, and release workflows.
