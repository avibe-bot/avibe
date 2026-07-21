# Memory MVP: Local EverOS Integration

> Status: scoped design, pending phase-0 POC
>
> Provider decision: provisional; see
> `docs/plans/memory-mvp/memory-plugin-product-research.md`
>
> Technical design: `docs/plans/memory-mvp/memory-plugin-everos-phase1-tech.md`

## 1. Outcome

The MVP lets the local Avibe owner enable personal memory, have eligible
Workbench messages distilled in the background, inspect a generated profile,
search past memory directly, see whether processing is healthy, and clear all
local Memory state.

It is one vertical slice for proving product value. It is not the complete
cross-platform Memory product.

## 2. Product contract

### 2.1 Supported user and surface

The MVP supports exactly one memory principal: the owner of this Avibe install.

Memory routes and settings are available only from a direct same-origin
loopback Workbench request with the existing CSRF proof. Network, Avibe Cloud,
LAN, proxy, IM, group, scheduled-task, harness, and agent-to-agent access are
not supported.

This restriction is a scope decision, not a claim that loopback proves a human
identity or protects against code already running as the same OS user. Avibe
agents and terminals already have broad local file access. The MVP protects the
supported product routes from accidental remote release; it is not a local
sandbox.

### 2.2 Enablement

The owner opens the Memory settings page, configures explicit
OpenAI-compatible LLM and embedding endpoints, reviews the disclosure, and
enables Memory.

Before enablement succeeds, Avibe:

- validates both endpoint blocks without logging or returning their keys;
- provisions the pinned sidecar runtime in the effective Avibe home;
- creates and verifies a dedicated owner-only Memory root;
- starts the sidecar and passes a health probe;
- records one local owner principal and one Memory epoch;
- opens capture admission only after those steps succeed.

If setup fails, Memory remains disabled and ordinary chat is unaffected.

### 2.3 Capture

The capture unit is one committed Workbench user message.

The MVP captures only normalized user-authored text. It does not capture:

- assistant responses;
- recalled memory content;
- framework metadata or system prompts;
- tool calls or tool output;
- attachment bytes, file paths, OCR, or derived attachment summaries;
- Memory commands;
- empty, command-only, or over-limit input.

Capturing user text only is deliberate. It removes the memory-feedback problem
and keeps every agent backend outside the MVP. Whether assistant context
materially improves memory quality is a future POC, not a hidden phase-1
requirement.

After the ordinary Workbench user row is committed, the route first proves the
same loopback-owner and CSRF facts required by every other Memory operation. The
shared capture hook then submits that server-created owner grant and a bounded
`CaptureRequest` to `MemoryModule`. Remote or otherwise unsupported Workbench
messages do not invoke the hook. The module performs only a local idempotent
queue insert on this path. It never waits for EverOS or a model endpoint, and a
Memory failure never blocks the agent turn.

The source message id is the local idempotency key. Submitting the same committed
message twice creates one local queue row while its content-free tombstone is
retained: 90 days, bounded to the newest 100,000 terminal rows. A replay outside
that window can enqueue again. A process crash before the local queue insert can
miss that message; a timeout around the later provider call can duplicate
provider-derived memory. These are accepted MVP limits and are reported as
aggregate status, not repaired through provider-internal evidence inspection.

### 2.4 Background processing

A controller-owned worker drains the local queue. For each row it:

1. maps the install, principal, and Workbench session to fixed EverOS fields;
2. sends the text through the internal EverOS port;
3. flushes that capture so direct queries do not depend on backend session-close
   events;
4. marks the local row delivered after the public provider operations succeed;
5. retries a bounded number of times on a closed retryable error, then marks the
   row dead and shows it in status.

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

Search and profile responses return only on the dedicated HTTP response with
`Cache-Control: no-store`. They do not create an `AgentRequest`, ordinary
`messages` row, Workbench transcript event, global SSE event, inbox/search row,
push notification, or IM response.

Provider text is rendered as inert text. The MVP does not interpret HTML,
Markdown actions, links, mentions, files, quick replies, or platform directives
from a Memory result.

### 2.6 Query behavior

Search sends the normalized query to the configured Memory embedding endpoint.
Profile reads may use only local provider state, depending on the pinned provider
route. No result is sent to Claude, Codex, OpenCode, or their model providers.

Explicit reads have fixed limits for query bytes, result bytes, item count, and
deadline. An invalid or oversized provider response fails with a closed error;
the UI never renders a partial unvalidated body.

The MVP does not promise per-item source links. It shows provider-reported dates
and kinds when valid and labels the result as distilled from the owner's
conversation history. It never fabricates provenance that the provider does not
expose.

### 2.7 Disable and re-enable

Disable closes new capture and direct reads, stops the worker after its current
request returns or times out, and stops the sidecar. Existing queued rows and the
provider root remain on disk.

Re-enable restarts the verified runtime and resumes the same queue. There is no
separate drain/discard state machine in the MVP. An owner who does not want
pending text processed uses Clear all before re-enabling.

### 2.8 Clear all

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

Startup resumes a persisted `clearing` state before opening Memory. Retrying
Clear all after a lost response is safe because clearing an already-empty owned
root has the same result.

Clear all removes:

- visible distilled Markdown;
- hidden EverOS buffers, MemCells, and indexes under the provider root;
- pending and terminal Memory queue rows;
- Memory-only operational metadata for the prior epoch.

It does not remove:

- original Avibe chat history;
- existing logs or crash reports;
- remote model-provider retention;
- user-created copies or filesystem snapshots;
- data already outside the dedicated Memory root;
- physical storage remanence.

The confirmation states these limits. The MVP does not inspect or delete Avibe
migration backups and does not claim forensic secure erase.

## 3. Data and processing disclosure

Before enablement, the owner is told:

- eligible Workbench user text is copied into a bounded local delivery queue;
- a row that exhausts its retry budget retains its failed plaintext for 14 days
  for local status/debugging, then scrubs the text; its content-free idempotency
  tombstone may remain for 90 days within the 100,000-row bound;
- the Memory LLM and embedding endpoints are separate from agent subscriptions;
- a remote Memory endpoint receives captured text or search queries and may
  retain them under its own policy;
- EverOS retains unflushed raw messages and extracted raw MemCells in hidden
  local SQLite state in addition to visible Markdown;
- disable freezes local state rather than deleting it;
- Clear all removes the dedicated local Memory root but cannot retract external
  provider copies or original chat history;
- provider delivery is at-least-once and rare duplicate derived memories are
  possible after timeout or crash.

Keys are write-only in the UI and stored with Avibe's owner-only config
permissions. Memory logs contain identifiers, counts, latency, and closed error
codes only, never prompts, queries, results, keys, headers, or provider bodies.

## 4. Scope mapping

The MVP is one global personal memory:

| Avibe concept | EverOS field | MVP value |
|---|---|---|
| Install | `app_id` | `avibe` |
| Personal memory | `project_id` | `personal` |
| Owner | `user_id` / user sender | locally generated principal UUID |
| Workbench conversation | `session_id` | keyed digest of local session id + epoch |
| Agent track | `agent_id` | unused |

Raw Workbench session ids do not enter provider paths. A local scope key derives
the bounded provider session reference.

There is no `workspace_id`, workspace partition, group scope, profile sharing,
or Plan-B switch in the MVP. A future workspace requirement reopens the scope
model and issue #320 decision rather than preallocating fields now.

## 5. Runtime and storage

The provider runs in a dedicated version-pinned Python environment and uses a
dedicated Memory root under the effective Avibe home. The environment is a
sibling of the provider root so Clear all cannot remove runtime code.

The sidecar binds only to an owner-only Unix-domain socket. It is not exposed
through Avibe tunnels or a TCP port. This prevents browser network access; it
does not protect against same-user local code, which is already inside Avibe's
desktop trust model.

The MVP supports the same POSIX desktop environments on which the pinned runtime
and Unix-domain socket pass the phase-0 and integration tests. It does not add a
new filesystem allowlist or promise stronger power-loss durability than Avibe's
normal SQLite state. Unsupported native Windows behavior is reported honestly
after testing rather than inferred in the design.

The local queue has fixed internal caps:

- 500 nonterminal capture rows;
- 64 MiB total plaintext payload;
- 32 KiB normalized text per capture;
- three provider attempts on a fixed backoff;
- 2 GiB provider-root warning threshold;
- 512 MiB low-free-space admission threshold.

These are implementation safety constants, not an initial user configuration
surface. At a queue or disk threshold, new Memory capture is skipped and chat
continues. Status shows aggregate missed/dead counts.

## 6. Failure behavior

| Failure | Memory behavior | Chat behavior |
|---|---|---|
| Sidecar unavailable | queue remains pending; bounded retry | unaffected |
| Processing endpoint unavailable | queue remains pending, then dead after retry budget | unaffected |
| Local queue full or disk low | skip new capture and increment aggregate count | unaffected |
| Provider response invalid or oversized | fail request/attempt with closed code | unaffected |
| Search timeout | show a direct read error | no agent turn is created |
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

The page has no capture matrix because Workbench loopback is the only source. It
has no auto-recall toggle, owner registry, remote enrollment, workspace selector,
provider selector, item editor, export/import, or advanced limit controls.

## 8. Acceptance criteria

The MVP is releasable to a local experimental cohort when:

1. the phase-0 POC selects official EverOS or an explicitly scoped Avibe fork;
2. a loopback owner can enable, send a Workbench message, observe indexing,
   search the resulting memory, view the profile, and clear all local Memory
   state end to end;
3. duplicate submission of one source message creates one local queue row within
   the documented 90-day/100,000-terminal-row idempotency window;
4. provider/model failure never blocks or changes the ordinary agent response;
5. all Memory HTTP routes reject non-loopback, missing-CSRF, and unsupported
   subjects before reading provider data;
6. Memory results never enter agent backends or ordinary transcript/event paths;
7. queue, input, response, deadline, and disk guards are tested at their bounds;
8. process restart recovers pending rows and an interrupted clear;
9. logs and serialized errors contain none of the fixture text or credentials;
10. the UI copy matches observed raw retention and at-least-once behavior.

## 9. Delivery order

### Slice 0: provider evidence

Run `docs/plans/memory-mvp/memory-poc-everos.md` and make the provider decision. No
production Memory interface is frozen before this gate.

### Slice 1: one local vertical path

Implement, behind an experimental feature flag:

- Memory config and local-only settings route;
- sidecar lifecycle;
- `MemoryModule` and dedicated Memory store;
- Workbench user-text capture hook;
- worker add + flush path;
- profile, search, status, and Clear all routes;
- minimal local UI;
- focused module/interface, store, route, and sidecar integration tests.

This slice is valuable on its own and is the complete MVP.

### Slice 2: experimental validation

Run synthetic and opt-in dogfood evaluation, measure queue/provider behavior,
and decide whether to harden EverOS, maintain a fork, or stop. Do not add broader
surfaces until the local product is useful.

## 10. Deferred capabilities

Each item below requires a separate design and evidence gate:

- private IM capture and direct commands;
- automatic recall into agent prompts;
- agent-facing `vibe memory` commands;
- assistant-message capture;
- group, network, or Cloud owner access;
- workspace isolation or shared memory;
- item-level deletion and profile rebuild;
- export/import and provider migration;
- foresight and editable Markdown guarantees;
- exactly-once provider delivery;
- configurable retention and resource policies.

The MVP interface must not contain placeholder fields or methods for these
capabilities.
