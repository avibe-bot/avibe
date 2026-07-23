# Memory MVP: Local EverOS Integration

> Status: scoped design, pending phase-0 POC
>
> Provider decision: provisional; see
> `docs/plans/memory-mvp/memory-plugin-product-research.md`
>
> Technical design: `docs/plans/memory-mvp/memory-plugin-everos-phase1-tech.md`

## 1. Outcome

The MVP lets the local Avibe owner enable personal memory, have eligible
Workbench and private-IM user messages distilled in the background, inspect a
generated profile, search past memory directly from the Memory page, `/memory`,
or `vibe memory`, see whether processing is healthy, and clear all local Memory
state from the UI.

It is one vertical slice for proving product value. It is not the complete
cross-platform Memory product.

The everyday flow is intentionally simple:

1. The owner installs the Memory runtime from Dependencies and enables Memory.
2. Eligible Workbench messages and administrator private messages are copied to
   a local queue. The normal chat reply does not wait for memory processing.
3. Avibe processes the queue in the background. A temporary process or endpoint
   outage pauses processing; it does not spend each message's retry budget.
4. The owner checks the result from the Memory page, `/memory`, or
   `vibe memory`.
5. Disable pauses the feature and keeps its data. Clear all deletes the local
   Memory data owned by this installation.

Memory is a built-in Avibe capability like Vaults: its page belongs in the
Workbench capability navigation and its local packages/binaries belong in
Settings -> Dependencies. It is not an App Library app, and EverOS is not a
user-facing plugin contract.

## 2. Product contract

### 2.1 Supported user and surface

The MVP supports exactly one provider principal and one `personal` pool for this
Avibe install. Bound, enabled administrator identities in supported private IM
conversations are explicit co-owners of that pool. This is an installation-level
authorization rule, not proof that every administrator identity is the same
human and not a multi-user isolation model.

In this MVP, "private IM" means a one-to-one conversation with a bound, enabled
administrator. Capturing ordinary member DMs into the same pool would let
administrators read another person's private history; supporting that requires
consent and per-user scope rather than a broader interpretation of this MVP.

Settings, the Memory page, Clear all, and Workbench `/memory` are available
only from a same-origin request whose TCP peer and effective host are both
direct loopback, whose mutation has the existing CSRF proof, and which carries
no forwarded/proxy metadata. This requires a new Memory-specific direct-loopback
predicate; Avibe's broader `_is_local_request()` helper is not sufficient. Local
Workbench currently has no separate login identity proof, so this relies on the
MVP's one-install/one-OS-account owner model rather than claiming an existing
authenticated human session.

`vibe memory` is available to processes running as the same OS account through
Avibe's existing mode-`0600` controller socket. Private-IM capture and `/memory`
are available only after the existing centralized IM authorization succeeds and
the server freshly proves `is_dm`, bound, enabled, and administrator status.
Memory admission fails closed when settings or identity state cannot be read;
administrator promotion, revocation, disablement, and unbinding take effect on
the next request. When Memory is enabled, an eligible interactive Workbench
owner or freshly admitted administrator-DM turn advertises the same read-only
`vibe memory` CLI to the agent. Group IM, non-administrator DM, Avibe Cloud,
LAN, proxy, scheduled-task, harness, and agent-to-agent turns do not receive
that guidance.

These restrictions are scope decisions, not claims that loopback, CSRF, a UDS,
or a private-chat topology proves one human identity. Avibe agents and terminals
already have broad local file access, and enabled IM administrators are treated
as trusted co-owners by product contract. The MVP gates which surfaces advertise
and invoke Memory; it is not a local sandbox against same-user code or an
identity federation system.

### 2.2 Enablement

Settings -> Dependencies always shows one `memory-runtime` row. The row names
the Avibe capability, not every EverOS, Python, LanceDB, Arrow, or other
transitive package. Its observed status is `ready`, `missing`, `unsupported`, or
`error`; the UI may temporarily show `installing` while its existing background
job runs. It is optional while Memory is disabled and required while Memory is
enabled.

Avibe owns installation and repair. The managed installer downloads an exact,
platform-qualified runtime artifact, verifies its manifest and digest, stages
it outside the active path, verifies its embedded Python, package lock, native
imports, and CLI identity, then atomically activates it. The user is not
required to install Python 3.12 or resolve Python packages. "Latest" means the
runtime pinned and declared compatible by the installed Avibe version, never
the newest arbitrary EverOS or PyPI release.

The owner then opens the Memory settings page, configures explicit
OpenAI-compatible LLM and embedding endpoints, reviews the disclosure, and
enables Memory. Enablement is a separate explicit action from installation. If
the managed runtime is not ready, the request returns `dependency_not_ready` and
the page offers the same background install/repair job as Settings ->
Dependencies. Memory stays disabled; after the dependency reports `ready`, the
owner explicitly retries Enable. Avibe does not persist a pending enable intent.
Package installation never runs synchronously on the HTTP request path and does
not live inside `MemoryModule`.

Before enablement succeeds, Avibe:

- validates both endpoint blocks without logging or returning their keys and
  performs one small authenticated request against the configured LLM and
  embedding services;
- proves that the Avibe-managed `memory-runtime` is compatible and ready;
- creates and verifies a dedicated owner-only Memory root;
- starts the sidecar and passes a health probe;
- records one local owner principal and one Memory epoch;
- discloses that every current enabled IM administrator can write and read the
  same pool, and that future administrator changes change that access;
- opens capture admission only after those steps succeed.

If dependency installation or setup fails, Memory remains disabled, the
Dependencies page offers repair, and ordinary chat is unaffected.

### 2.3 Capture

The capture unit is one accepted, stable human-text message from either the
Workbench or an eligible private IM conversation.

The MVP captures only normalized user-authored text. It does not capture:

- assistant responses;
- recalled memory content;
- framework metadata or system prompts;
- tool calls or tool output;
- attachment bytes, captions, file paths, OCR, or derived attachment summaries;
- rich, forwarded, shared, edited, system, bot, or self-authored events;
- Memory commands;
- empty, command-only, or over-limit input.

Capturing user text only is deliberate. It removes the memory-feedback problem
and keeps agent responses and tool output outside capture. Whether assistant
context materially improves memory quality is a future POC, not a hidden
phase-1 requirement.

Each entry applies its own admission policy before the shared capture seam:

- Workbench captures only a committed user row after the direct-loopback,
  no-forwarded-metadata, and CSRF checks. Its UI process submits bounded
  committed-row fields over Avibe's existing controller UDS.
- Private IM captures only an ordinary human text turn after platform DM
  classification, centralized bind/enable authorization, the administrator
  co-owner check, command interception, and native-message deduplication. The
  shared `MessageHandler` owns this policy; platform adapters provide normalized
  context but do not implement Memory business logic.

After the entry adapter authorizes the request, the controller creates a
`CaptureRequest` and calls `MemoryModule`. The module performs only a local
idempotent queue insert on this path. It never waits for EverOS or a model
endpoint, and a Memory failure never blocks or changes the ordinary agent turn.

The module derives the local idempotency key as a keyed digest of the
source-qualified message id: Workbench supplies its local committed id and IM
supplies the canonical `(platform, native_message_id)` pair. Raw source ids are
not stored in the Memory database and equal ids from different platforms never
share a key. Submitting the same accepted message twice creates one local queue
row while its content-free tombstone is retained: 90 days, bounded to the newest
100,000 terminal rows. A replay outside that window can enqueue again. A process
crash before the local queue insert can miss that message; a timeout around the
later provider call can duplicate provider-derived memory. These are accepted
MVP limits and are reported as aggregate status, not repaired through
provider-internal evidence inspection.

### 2.4 Background processing

A controller-owned worker drains the local queue. For each row it:

1. maps the install, principal, and source conversation to fixed EverOS fields;
2. sends the text through the internal EverOS port;
3. flushes that capture so direct queries do not depend on backend session-close
   events;
4. marks the local row delivered after the public provider operations succeed;
5. retries a bounded number of times when that message cannot be processed,
   then marks the row dead, immediately scrubs its text, and shows only aggregate
   status.

A sidecar crash or a processing endpoint outage is different from a bad message.
It pauses new queue claims and leaves queued text pending. Avibe supervises and
restarts the sidecar with bounded backoff. Message-level retries are used only
after the runtime and endpoints are reachable and one message still fails.
When an EverOS error does not reliably say which case occurred, Avibe checks the
sidecar and performs small authenticated probes against both configured model
endpoints. A failed probe keeps the queue paused without spending that row's
retry budget; healthy probes make a repeated failure count against only that
row, so one permanently failing message cannot hold the queue forever.

This is at-least-once delivery. The MVP does not promise exactly-once derived
facts or recovery from a crash at every provider write edge.

### 2.5 Direct Memory view

The local Workbench Memory view provides:

- **Profile:** the provider's current user profile in a bounded text view.
- **Search:** a bounded query over profile, episodes, and facts.
- **Status:** runtime health, queue counts, last successful processing time,
  provider-root size, and last closed error category.
- **Clear all:** one confirmed operation that removes local provider memory and
  Avibe's pending Memory work.

Memory-page search and profile responses return on a dedicated HTTP response
with `Cache-Control: no-store`. They do not create an `AgentRequest`, ordinary
`messages` row, Workbench transcript event, global SSE event, inbox/search row,
push notification, or IM response. Workbench command results are likewise
ephemeral. A private-IM command result is the explicit exception: it bypasses
the agent, Avibe transcript, and Memory capture but is sent as a new bot reply in
that private conversation and may be retained, notified, or synchronized by the
IM platform.

Direct UI and command provider text is rendered as inert text. The CLI emits
plain text or bounded JSON. Private-IM replies are additionally bounded to the
platform-safe response limit and sent without Markdown actions, active mentions,
link previews, files, quick replies, or platform directives. No entry adapter
interprets provider content before returning it.

### 2.6 Explicit command and CLI reads

The three explicit read adapters deliberately expose one vocabulary:

```text
Workbench: /memory [help|status|profile|search <query>]
Private IM: /memory [help|status|profile|search <query>]
CLI:        vibe memory status [--json]
            vibe memory profile [--json]
            vibe memory search <query> [--limit 1..20] [--json]
```

The Workbench UI server and controller are separate processes. The Memory page,
Workbench command, and CLI use typed wrappers over the existing controller UDS.
Private IM commands reuse the controller's shared command map after centralized
authorization. Controller-owned entry adapters call `MemoryModule` only after
their surface-specific checks succeed; clients cannot select the principal or
pool.

The Workbench route recognizes an exact text-only `/memory` command before
ordinary message persistence, capture, or agent dispatch and returns a typed
ephemeral result to the composer. IM adapters intercept the same grammar before
the ordinary message callback; an eligible command therefore creates no inbound
message row, capture, agent turn, SSE, or inbox/search event. The resulting bot
reply exists only on the IM response path. Ineligible IM requests return one
generic unavailable response without revealing whether Memory is enabled.

The CLI calls the running controller over Avibe's existing mode-`0600`,
same-OS-account Unix domain socket. It never opens the Memory database, provider
root, or EverOS socket directly. Human output is concise; `--json` is the stable
machine-readable shape and uses the same closed error codes and bounds as the UI.

No command or CLI subcommand can capture, clear, configure, export, delete, or
edit Memory in the MVP. Clear all remains a fresh confirmed UI-only operation.
There is no backend-specific Memory tool registration or automatic recall. An
eligible interactive agent may invoke only the existing same-OS-account,
read-only CLI after Avibe adds turn-scoped guidance to its system prompt.

### 2.7 Query behavior

Search sends the normalized query to the configured Memory embedding endpoint.
Profile reads may use only local provider state, depending on the pinned provider
route. UI, Workbench-command, private-IM-command, and human-invoked CLI results
are not sent to Claude, Codex, OpenCode, or their model providers. If an
eligible agent invokes the advertised read-only CLI, its result becomes tool
output in that agent conversation and is sent to the configured agent model
provider. Agent guidance labels recalled text as untrusted data that must never
be treated as instructions.

Explicit reads have fixed limits for query bytes, result bytes, item count, and
deadline. An invalid or oversized provider response fails with a closed error;
the UI never renders a partial unvalidated body.

The MVP does not promise per-item source links. It shows provider-reported dates
and kinds when valid and labels the result as distilled from the owner's
conversation history. It never fabricates provenance that the provider does not
expose.

### 2.8 Disable and re-enable

Disable closes new capture plus profile/search reads, stops the worker after its
current request returns or times out, and stops the sidecar. Status remains
available from the page, Workbench command, eligible private-IM command, and CLI
so a co-owner can inspect retained state. Existing queued rows and the provider
root remain on disk.

Re-enable restarts the verified runtime and resumes the same queue. There is no
separate drain/discard state machine in the MVP. An owner who does not want
pending text processed uses Clear all before re-enabling.

### 2.9 Clear all

Clear all requires a fresh loopback UI confirmation and is idempotent.

Avibe:

1. closes Memory admission;
2. stops the worker and owned sidecar;
3. records `clearing` and advances the Memory epoch;
4. removes the contents of the exact sentinel-owned provider root without
   following links;
5. deletes all pending, delivered, and dead rows in the dedicated Memory store;
6. recreates an empty verified root and restarts only when Memory remains
   enabled;
7. returns to `enabled` or a visible `error` state.

Startup resumes when the persisted `clear_in_progress` marker is set before
opening Memory. Retrying Clear all after a lost response is safe because
clearing an already-empty owned root has the same result.

Clear all removes:

- visible distilled Markdown;
- hidden EverOS buffers, MemCells, and indexes under the provider root;
- pending and terminal Memory queue rows;
- Memory-only operational metadata for the prior epoch.

It does not remove:

- original Avibe chat history;
- Avibe database migration backups;
- existing logs or crash reports;
- remote model-provider retention;
- user-created copies or filesystem snapshots;
- data already outside the dedicated Memory root;
- physical storage remanence.

The confirmation states these limits. The MVP does not inspect or delete Avibe
migration backups and does not claim forensic secure erase.

## 3. Data and processing disclosure

Before enablement, the owner is told:

- eligible Workbench and bound, enabled administrator-DM user text is copied
  into a bounded local delivery queue;
- a row that succeeds or exhausts its retry budget immediately scrubs its text;
  its content-free idempotency tombstone may remain for 90 days within the
  100,000-row bound;
- the Memory LLM and embedding endpoints are separate from agent subscriptions;
- a remote Memory endpoint receives captured text or search queries and may
  retain them under its own policy;
- private IM text has already traversed that platform before local capture, and
  an IM `/memory` query plus its bounded result remains subject to the platform's
  chat-history, notification, device-sync, and retention policy;
- EverOS retains unflushed raw messages and extracted raw MemCells in hidden
  local SQLite state in addition to visible Markdown;
- disable freezes local state rather than deleting it;
- Clear all removes the dedicated local Memory root but cannot retract external
  provider copies or original chat history;
- provider delivery is at-least-once and rare duplicate derived memories are
  possible after timeout or crash.

Keys are write-only in the UI and stored with Avibe's owner-only config
permissions. The sidecar receives them only through its child-process
environment; generated provider files contain no keys. Memory logs contain
identifiers, counts, latency, and closed error codes only, never prompts,
queries, results, keys, headers, or provider bodies.

## 4. Scope mapping

The MVP is one global personal memory:

| Avibe concept | EverOS field | MVP value |
|---|---|---|
| Install | `app_id` | `avibe` |
| Personal memory | `project_id` | `personal` |
| Owner | `user_id` / user sender | locally generated principal UUID |
| Source conversation | `session_id` | keyed digest of source-qualified conversation id + epoch |
| Agent track | `agent_id` | unused |

Raw Workbench session ids and raw IM platform/user/chat ids do not enter provider
paths. A local scope key derives the bounded provider session reference from a
source-qualified conversation identity.

There is no `workspace_id`, workspace partition, group scope, profile sharing,
or Plan-B switch in the MVP. A future workspace requirement reopens the scope
model and issue #320 decision rather than preallocating fields now.

## 5. Runtime and storage

The provider runs from one Avibe-managed, immutable `memory-runtime` artifact
with an embedded compatible Python and exact package lock. It uses a separate
dedicated Memory root under the effective Avibe home. Runtime code and provider
data never share a deletion root, so Clear all cannot remove installed packages
or binaries.

The Dependencies page is the source of truth for install, version,
compatibility, and repair state. The Memory page owns model configuration,
disclosure, enable/disable, status, profile/search, and Clear all. Installing the
runtime does not enable capture, create a provider principal, start a persistent
sidecar, or require model credentials.

An Avibe release must declare whether a new Memory runtime can open the existing
provider data root. A compatible upgrade keeps the data. If compatibility is
unknown or explicitly broken, Avibe keeps the previous verified runtime active
and asks the owner to Clear all before activating the new one; it never tries an
unannounced in-place migration.

Startup dependency reconciliation is best effort and cannot delay Avibe startup.
When Memory is disabled and the runtime has never been installed, Avibe does not
download it. When Memory is enabled, a missing, incompatible, or broken runtime
keeps Memory unavailable while ordinary chat starts normally and the dependency
status exposes the repair action.

The sidecar binds only to an owner-only Unix-domain socket. It is not exposed
through Avibe tunnels or a TCP port. This prevents browser network access; it
does not protect against same-user local code, which is already inside Avibe's
desktop trust model.

The managed runtime includes only the verified Python distribution and locked
EverOS dependencies. The Avibe package supplies a small child-only launcher,
which the managed Python loads from the installed Avibe source path. The
launcher loads the pinned EverOS application entry point and starts it on the
Unix socket. The parent Avibe process does not import EverOS or read its private
storage. This versioned launcher is the only allowed package-level integration
and is tested against each runtime artifact.

The MVP supports the same POSIX desktop environments on which the pinned runtime
and Unix-domain socket pass the phase-0 and integration tests. It does not add a
new filesystem allowlist or promise stronger power-loss durability than Avibe's
normal SQLite state. Unsupported native Windows behavior is reported honestly
after testing rather than inferred in the design.

The local queue has fixed internal caps:

- 500 nonterminal capture rows;
- 32 KiB normalized text per capture;
- three message-level attempts on a fixed backoff;
- 2 GiB provider-root warning threshold;
- 512 MiB low-free-space admission threshold.

These are implementation safety constants, not an initial user configuration
surface. At a queue or disk threshold, new Memory capture is skipped and chat
continues. Status shows aggregate missed/dead counts.

## 6. Failure behavior

| Failure | Memory behavior | Chat behavior |
|---|---|---|
| IM identity/settings missing, stale, or no longer administrator | fail closed; no capture/read and no enabled-state disclosure | ordinary non-command turn continues; `/memory` gets generic unavailable |
| Managed runtime missing, incompatible, or broken | Memory remains unavailable; Dependencies exposes install or repair | unaffected |
| Runtime install or repair fails | keep the previous verified runtime inactive; return a closed dependency error | unaffected |
| Sidecar unavailable | pause queue claims; restart with bounded backoff; queued text remains pending | unaffected |
| Processing endpoint unavailable | pause queue claims and expose the endpoint error; queued text remains pending | unaffected |
| One message fails while the runtime and endpoints are healthy | retry that message three times, then mark it dead and erase its text | unaffected |
| Local queue full or disk low | skip new capture and increment aggregate count | unaffected |
| Provider response invalid or oversized | fail request/attempt with closed code | unaffected |
| Search timeout | return a bounded UI/command/CLI error | ordinary chat is unaffected |
| Crash during provider call | reclaim local row and retry at least once; duplicate possible | unaffected after restart |
| Clear interrupted | startup resumes idempotent clear before Memory opens | ordinary chat remains available |
| Runtime restart after clear fails | local clear remains complete; Memory shows error | unaffected |

No failure path returns raw provider errors or silently claims that asynchronous
profile/fact generation succeeded.

## 7. UI surface

The MVP adds one local Memory settings/view surface containing:

- enable/disable;
- LLM endpoint, model, and write-only key;
- embedding endpoint, model, and write-only key;
- the full processing and retention disclosure;
- profile and search tabs;
- status and bounded queue/storage counters;
- confirmed Clear all.

The page is the only configuration and destructive-governance surface.
Workbench `/memory`, eligible private-IM `/memory`, and the CLI expose only
profile, search, and status.

Changing the embedding endpoint or model while memory data exists is rejected;
the owner runs Clear all first so one index never mixes incompatible vector
spaces.

The page has no separate capture matrix or owner registry. Workbench eligibility
is fixed by the direct-loopback contract; private-IM eligibility reuses the live
bound, enabled administrator role. The page discloses the current co-owner rule
but does not add identity linking, remote enrollment, per-user pools, an
auto-recall toggle, workspace selector, provider selector, item editor,
export/import, or advanced limit controls.

When the same page is opened through Avibe Cloud or another unsupported remote
route, it shows a static "available on this device only" state. It does not load
or reveal Memory enablement, profile, queue, or storage data.

The MVP intentionally gives all enabled IM administrators one shared profile.
Dogfood must record conflicting profile items caused by different administrators
as a named quality metric. This observation does not add per-user pools to the
MVP.

## 8. Acceptance criteria

The MVP is releasable to a local experimental cohort when:

1. the phase-0 POC selects official EverOS or an explicitly scoped Avibe fork;
2. a loopback owner can install or repair `memory-runtime` from Dependencies
   without a system Python 3.12, and Clear all leaves that runtime intact;
3. a loopback owner can enable, send a Workbench message, observe indexing,
   search the resulting memory, view the profile, and clear all local Memory
   state end to end;
4. a bound, enabled administrator can send ordinary text in a supported private
   IM, observe the same pool being indexed, and query it with `/memory`;
5. unbound, disabled, non-administrator, group, scheduled, bot/self, attachment,
   forwarded, and Memory-command inputs produce no capture or provider call;
6. `/memory status`, `/memory profile`, and `/memory search <query>` return typed
   ephemeral Workbench results or bounded inert private-IM replies without
   creating a capture or agent turn;
7. `vibe memory status|profile|search` reaches the live controller only through
   the verified mode-`0600` same-account internal socket, and `--json` has a
   tested stable schema;
8. duplicate submission of one source-qualified message digest creates one local queue
   row within the documented 90-day/100,000-terminal-row idempotency window, and
   equal native ids from two platforms remain distinct while raw ids are not
   stored in the Memory database;
9. provider/model failure never blocks or changes an ordinary agent
   response;
10. every Memory HTTP route rejects non-direct-loopback, forwarded, and
   unsupported requests before reading provider data, and every mutation rejects
   missing or invalid CSRF;
11. UI, Workbench-command, private-IM-command, and CLI results never enter agent
    backends or capture; only the explicit IM result enters platform chat history;
12. status remains readable but profile/search stay closed while Memory is
    disabled on all four read surfaces;
13. administrator promotion, revocation, disablement, unbinding, and missing
    settings state are re-evaluated fail-closed before every IM read/capture;
14. queue, input, response, deadline, and disk guards are tested at their bounds;
15. a sidecar or endpoint outage pauses queue claims without consuming message
    retry budgets; an ambiguous failure uses health probes to make the same
    decision, while one permanently failing message eventually becomes dead and
    unblocks later rows; process restart recovers pending rows and an interrupted
    clear;
16. logs and serialized errors contain none of the fixture text or credentials;
17. the UI copy matches observed raw retention, immediate terminal payload
    scrubbing, IM-platform retention, co-owner
    access, and at-least-once behavior;
18. a generic non-Memory config save round-trip preserves the memory
    configuration block and its stored keys.

## 9. Delivery order

### Slice 0: provider evidence

Run `docs/plans/memory-mvp/memory-poc-everos.md` and make the provider decision. No
production Memory interface is frozen before this gate.

### Slice 1: storage and module

Implement the five-method `MemoryModule`, two-table dedicated store, queue
worker, clear-recovery marker, fake provider, and module/store contract tests.

### Slice 2: local provider and settings

Implement the managed `memory-runtime` dependency by extending the shared
managed-runtime Module, the selected provider port, private sidecar lifecycle,
V2 config/live reconciliation, direct-loopback
settings/routes, disclosure, status, and Clear all. Extend the existing
Dependencies status/install-job surface rather than introducing a Memory-only
package installer.

### Slice 3: Workbench, private IM, and CLI vertical path

Add committed Workbench capture, shared private-IM admission and capture,
profile/search/status UI, the exact Workbench and IM `/memory` intercepts, local
`vibe memory` reads, five-platform contract tests, and direct-path scenario/Incus
tests behind the experimental feature flag.

### Slice 4: experimental validation

Run synthetic and opt-in dogfood evaluation, measure queue/provider behavior,
including conflicting profile items from multiple administrator identities, and
decide whether to harden EverOS, maintain a fork, or stop. Do not add broader
surfaces until the local product is useful.

## 10. Deferred capabilities

Each item below requires a separate design and evidence gate:

- **Registered agent-facing Memory tools.** This includes MCP transport,
  backend-specific tool registration, and OS-enforced turn-scoped grants. The
  MVP only advertises the existing same-account read-only CLI on eligible
  interactive turns; it does not claim the CLI is a local process sandbox.
- **Automatic recall into agent prompts.** Deferred until explicit reads prove
  memory usefulness and a prompt-injection/latency policy exists; hidden context
  would make relevance, cost, and provenance difficult to govern.
- **Capture, clear, configuration, export, or deletion through commands or CLI.**
  The MVP keeps command/CLI reads ephemeral and makes destructive operations UI
  confirmation flows, avoiding a second authorization and recovery surface.
- **Assistant-message capture.** Deferred because capturing model output can feed
  generated claims back into memory and requires provenance, feedback-loop, and
  native-context rules that user-text capture does not need.
- **Group, non-administrator DM, network, Cloud, and remote-owner access.** The
  private-IM MVP is a narrow administrator co-owner exception. General shared or
  remotely routed conversations still require membership, consent, tenancy, and
  egress design.
- **Per-user pools and cross-platform human-identity linking.** The MVP does not
  infer that administrator identities belong to one human; all enabled
  administrators intentionally share one install-level pool. Independent users
  require a new scope, migration, and governance contract.
- **Workspace isolation and shared memory.** Deferred because they require a
  durable scope model, migration semantics, and a provider identity contract;
  the MVP intentionally has one `project_id=personal` pool.
- **Item-level deletion and profile rebuild.** Deferred because the first provider
  does not expose reliable source-to-derived deletion or convergence guarantees;
  Clear all is the only honest recovery operation for now.
- **Export/import and provider migration.** Deferred until a provider-neutral
  format and provenance/version rules exist; exporting one provider's private
  Markdown or SQLite would create a false portability promise.
- **Foresight and editable Markdown guarantees.** Deferred because they add
  planning, edit conflict, and durable-index convergence semantics beyond basic
  profile/search value.
- **Exactly-once provider delivery.** Deferred because the provider exposes no
  stable write receipt or caller idempotency contract; the MVP documents local
  idempotency plus provider at-least-once behavior instead.
- **Configurable retention and resource policies.** Deferred until POC measurements
  establish useful defaults; exposing knobs before that would multiply lifecycle,
  disclosure, and test combinations without evidence.

The MVP interface must not contain placeholder fields or methods for these
capabilities.
