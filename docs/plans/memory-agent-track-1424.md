# EverOS Agent Memory Track

> Status: approved implementation contract
>
> Issue: [#1424](https://github.com/avibe-bot/avibe/issues/1424)
>
> Provider reference: EverOS 1.2.3

## Goal

Add an opt-in Agent Memory beta that turns eligible, successfully completed
Avibe Agent Turns into EverOS `agent_case` records and lets EverOS cluster those
cases into `agent_skill` records. The track is isolated from Personal Memory,
never delays Agent Turn settlement, and is recalled only through explicit,
bounded CLI or owner Settings operations.

This contract supersedes the earlier Agent-capture non-goal in
[`memory-plugin-system.md`](./memory-plugin-system.md). It does not change the
Personal Memory admission, prompt, provider root, queue, or retrieval semantics.

## Locked Decisions

1. The existing Personal Memory provider root is permanently a chat-mode root.
   Agent Memory uses a second root, UDS, generated-config directory, process
   ownership record, lifecycle supervisor, health projection, and retention
   slot. No reconcile or maintenance operation may mode-flip an existing root.
2. The durable source is `session_turns`, joined to `agent_sessions`, not
   Harness-specific `agent_runs`. The shared execution owner is extended to
   create these rows for Workbench and every supported IM transport as well as
   Harness dispatch; platform transcript mirroring is not a trajectory source.
   All interactive and Harness turns use the same eligibility rules. Each new
   Turn snapshots the exact executing `agents.id`; mutable Session routing is
   not execution identity.
3. A controller-owned background scanner copies eligible terminal turns into a
   separate schema-v4 queue. Terminal settlement and backend adapters do no
   Memory I/O and await no Memory work.
4. EverOS `agent_id` is derived from the Turn's immutable executing `agents.id`
   snapshot with the install-local Memory scope key. Mutable Session routing,
   Agent names, and user principals are never agent owners.
5. Agent/project retrieval is explicit, lazy, inert, and bounded. Agent Memory
   is never injected into a prompt, installed as a Codex/Claude skill, or
   executed.
6. The first version contains only the exact final backend dispatch text,
   durably snapshotted after all transformations and before native dispatch,
   plus the terminal result. It does not parse formatted event streams into
   synthetic tool calls.
7. Enabling the track does not create a project binding. The owner must bind a
   source workdir to either the exact `default` project or an existing/new-style
   named Memory project. Missing bindings fail closed. Binding additions,
   reassignments, and removals use commit-ordered high-water epochs.
8. EverOS 1.2.3 limitations listed below are accepted. Avibe isolates and
   reports them but does not patch EverOS or file an upstream issue. Retrieval
   exposes skill freshness/maturity metadata, and status adds a conservative
   per-Agent skill-count hint; neither mitigation mutates provider state.
9. Every Memory config capture/binding cutover first records its terminal-settlement
   high water and prepared recovery intent atomically in the primary state
   writer transaction, before the V2 config replacement. Recovery commits that
   exact boundary only when the persisted config has the intent's desired
   revision/digest pair, or cancels it when the prior pair remains. Clear and
   factory reset
   retain their documented primary-state destructive-recovery cutover. The
   Memory scan state is an idempotent projection of those records. There is no
   historical, disabled-period, or post-reset replay in v1; only turns admitted
   at dispatch and settling inside the same committed enabled epoch are
   candidates.

## Product Contract

### Enablement and disclosure

`memory.agent_track` is an optional persisted block with this exact shape:

```yaml
memory:
  agent_track:
    enabled: false
    project_bindings:
      - workdir: /normalized/absolute/path
        project_id: default
```

`enabled` is a boolean and defaults to `false`; `project_bindings` permits at
most 128 entries with unique normalized absolute workdirs paired with exact
new-style Memory project ids. Each UTF-8 workdir and project id is at most 4,096
bytes. Absence is equivalent to disabled with no bindings. The V2 config block
is the authoritative owner setting and survives Clear/factory reset. A malformed
optional `agent_track` block is discarded as a whole with a sanitized warning;
it must not prevent Avibe startup or alter the independently parsed Personal
Memory configuration.

The owner Settings surface explains that eligible Agent inputs and results may
be sent to the configured Memory processing endpoints, that accepted processing
may produce no case or skill, and that the feature inherits the documented
EverOS 1.2.3 limitations. The source set is fixed to eligible completed Agent
Turns from Workbench, Slack, Discord, Telegram, Feishu/Lark, WeChat, and Harness;
v1 has no source-type or platform selector.
The disclosure also states that the exact backend dispatch and final result are
trajectory content: user/Agent-authored identifiers and Avibe-injected time,
source-Session, username, or user-id attribution lines present in those texts
are retained locally and forwarded to the processing endpoints. They are never
used as structural Memory owners, project selectors, or provider paths.
The binding control also discloses that removal or reassignment stops eligibility
for later-settling Turns, while already-settled backlog through its committed
cutover remains eligible under the old project.

Enabling requires the installed Memory runtime, complete processing endpoints,
and at least one explicit project binding. Every writer that mutates any Memory
V2 config field, including Personal enablement, endpoint/API-key edits, Agent
enablement and bindings, and repair/rebuild settings, submits a field-scoped
patch plus its expected Memory-config revision to one authenticated controller
IPC transaction, `apply_memory_settings`. `apply_agent_memory_settings` is its
Agent-role logical operation, not an independent writer. No UI, compatibility,
or maintenance process directly persists any part of the Memory config. The
controller compares the expected revision, applies the patch to the latest
config, owns any capture/binding/config/claim transition, atomically replaces
V2 config, reconciles the affected roles, and retains the same cross-process
lifecycle/config transaction through intent cleanup. A stale revision returns a
conflict for reload instead of overwriting another writer; a process-local UI
lock is only a debounce and never a correctness boundary. If the controller is
unavailable, the request fails before any mutation.

For an Agent-role patch, the controller first uses one serialized primary state
transaction to read terminal high water `H` and insert a singleton prepared
`memory_agent_capture_cutover` intent with a new capture epoch, prior and desired
Agent-block digests, the whole Memory-config revision, desired enabled state,
and `H`. Terminal settlement cannot
interleave between that read and intent commit. The scanner is fenced, then the
desired V2 config is atomically replaced and the prepared intent is promoted to
committed. The cutover is conditionally sequence-effective at the prepared
intent's `H`: Turns above `H` use the desired state if the config replacement
succeeds, or the prior state if it fails.

Normal Agent scanner admission is fenced while a capture intent exists. A
prepared intent permits no projection or drain; after config persistence,
committed intent handling permits only idempotent cutover projection and the
bounded disable drain. Enable
projects an open epoch and cursor `H`, rebuilds opaque bindings, starts the role
runtime, and only then atomically persists the enabled current-capture
projection while clearing the intent and opening scanner/snapshot admission.
Disable
first closes new provider claims in the controller, commits its disabled intent,
then projects the old epoch's inclusive drain target `H`. It waits for any current
bounded provider request to settle before stopping the worker and sidecar; after
worker quiescence no provider write may start. The scanner runs local-only in
`disabling` mode until every source through `H` is enqueued or durably skipped,
then stops and atomically persists the disabled current-capture projection while
clearing the intent. Rows enqueued by the drain remain pending without provider
I/O and become claimable only after a later enable.

All Memory writers remain serialized behind that controller transaction until
the intent is cleared, so a Personal or endpoint save cannot create a third
digest during Agent recovery. If V2 config persistence fails, the still-prior
Agent-block digest and config revision authorize cancellation
of the prepared intent, any claims closed for a proposed disable reopen, and
scanner work resumes under the prior epoch, including Turns above `H`. After a
crash, recovery compares the persisted revision and Agent-block digest: the
prior state cancels the intent, the exact desired state promotes and projects
the intent's original epoch and `H`, and any unrecognized state fences Agent
Memory as degraded for explicit repair. The same controller transaction must
repair or retire that intent before accepting another Memory write. Recovery
never invents a later high water. Thus a
disable that reached config commit cannot extend its old enabled epoch to
restart time. Memory projection/drain and intent cleanup are idempotent.
Re-enable requires an earlier disable drain to converge, then its own enabled
cutover skips the completed opt-out interval. Clear/factory reset record the
same primary-state intent immediately before recreating scan state, after
destructive deletion is complete, and replace the current-capture projection at
the same fresh epoch before clearing that intent. Turn settlement performs no
Memory/config I/O and never waits for projection or drain work.

If Agent runtime/sidecar start fails after the desired enabled config and intent
commit, Agent Memory remains `enabled=true` but `degraded` with that exact
committed intent retained; it does not apply Personal Memory's failed-enable
rollback rule. The dispatch snapshot gate and scanner/provider claims remain
closed while any enable intent exists, so no trajectory text accumulates. The
controller's Agent-role Retry/Restart reuses the intent idempotently and clears
it only after the role is healthy. An explicit Disable may instead supersede the
failed enable through the normal controller cutover. Personal Memory and normal
Agent execution remain unchanged. The controller IPC returns a closed
`memory_agent_start_failed` degradation with `persisted_enabled=true` and
`recovery_pending=true`; Settings must not report either disabled rollback or
ready state.

### Admission

One durable `session_turns` row is one candidate trajectory. The scanner reads
in stable `terminal_sequence` order, joins the owning `agent_sessions` row for
source workdir, and uses immutable identity/source/text snapshots carried by the
Turn itself. Admission is provider-neutral and accepts a row only when all of
these invariants hold:

- the turn is terminal with `terminal_outcome="completed"`;
- `terminal_evidence_kind="terminal_result"` and the versioned evidence contains
  one non-empty final `result_text`;
- immutable `backend_dispatch_text` and the final result are valid, nonempty
  UTF-8 text of at most 256 KiB each;
- `backend_input_shape="text_only"`; any Turn whose native backend request also
  contains an out-of-band image, attachment, audio, file, or other non-text
  input is excluded even when its dispatch string is nonempty;
- the dispatch snapshot was admitted while one committed capture epoch was
  enabled, and the Turn settled inside that same epoch; a Turn crossing enable,
  disable, Clear, or reset is excluded;
- no accepted live-steer Delivery is linked to the Turn; v1 excludes steered
  Turns because their accepted instruction text is not durably available after
  materialization and cannot satisfy the exact two-message contract;
- the Turn carries a nonempty `executing_agent_id` that was validated against a
  real `agents.id` at start claim; legacy/pre-migration or backend-only Turns
  without that snapshot fail closed, while later Session rerouting or Agent
  rename/disable/archive/hard-delete does not rewrite the snapshot;
- `source_class` is exactly `interactive` or `harness_request`, never `callback`,
  `maintenance`, `agent_callback`, or missing; and
- the immutable start-snapshot `binding_key` plus `binding_epoch` resolves at the
  Turn's terminal sequence through that same continuous opaque epoch to one
  new-style Memory project.

Every Delivery snapshots one admission-relevant source class from durable
provenance. FIFO/scheduled compatibility keys include that class, and Turn claim
asserts that every constituent Delivery matches before writing the Turn's
`source_class`; an incompatible row remains queued for a later Turn. An ordinary
Harness request therefore cannot coalesce with a callback even when both use the
same trigger/backend/Session.

After scheduled attribution, transcription, attachment/file formatting, and all
other input transformations finish, the shared start boundary classifies the
final native request as the closed `text_only | out_of_band_attachment`
`backend_input_shape` and checks the controller-projected primary capture epoch
without opening the Memory store. The same primary transaction derives the
source workdir's opaque `binding_key` and requires it in the controller-projected
primary admission set at the current committed Memory-config revision, with no
binding cutover intent. That set contains only opaque keys, never workdirs or
projects, and returns the current monotonic `binding_epoch`; the controller
replaces it while the intent fence is closed and opens snapshot admission only
after V2 config and the projection converge. Only bound `text_only` requests are
candidates. If the capture epoch is committed enabled, the
exact UTF-8 text is at most 256 KiB, and the global primary snapshot budget
remains below 128 MiB, the transaction reserves its byte count and persists
`backend_dispatch_text`, digest, shape, opaque binding key/epoch, and capture
epoch before invoking the native adapter. The adapter must send exactly that
stored text with no additional native input; an automatic start retry reuses the
snapshot instead of re-running a text-changing transform. The scanner later
requires that exact binding generation at terminal settlement. A
reassignment/removal invalidates an in-flight generation, while a Turn started
before its first binding is never admitted retroactively.

Disabled/transitioning epochs, missing/transitioning bindings, out-of-band
attachment input, oversized text, or an exhausted snapshot budget record only a
closed omission reason, input-shape marker, and small counter; they never retain
text, attachment paths/bytes, or consume the snapshot budget or block native
dispatch. A successful config transition cannot retroactively
admit an in-flight Turn that omitted its snapshot. Persistence failure for a
snapshot that was admitted still fails closed before native start, because the
candidate could otherwise diverge from the adapter input. Backend-native
acceptance can therefore never precede the durable exact input used by an
Agent-Memory candidate.

The property is deliberately positive. Every terminal shape not satisfying the
complete invariant is skipped, including failed, canceled, stopped/restarted,
silent, missing-result, missing/guarded final-dispatch, epoch-crossing,
malformed-evidence, out-of-band attachment, oversized, accepted-steer,
callback-class, and unbound turns.
Admission logs only a closed
reason and opaque source digest.

The v1 trajectory has exactly two ordered messages:

```json
[
  {
    "sender_id": "i-<32 lowercase hex>",
    "role": "user",
    "timestamp": 0,
    "content": "<exact backend_dispatch_text>"
  },
  {
    "sender_id": "a-<32 lowercase hex>",
    "role": "assistant",
    "timestamp": 1,
    "content": "<exact terminal result_text>"
  }
]
```

The actual timestamps are stable millisecond values derived from the durable
turn times, with the assistant timestamp strictly later than the input. The
synthetic input actor is local to this isolated provider track and is never
treated as an Avibe user principal. There are no attachments, `tool_calls`, tool
results, reasoning traces, or sender names in v1.

### Identity and project isolation

The Memory store's install-local `scope_key` derives both opaque identities:

```text
agent_id    = "a-" + HMAC_SHA256(scope_key, "agent:" + agents.id).hex()[:32]
binding_key = "b-" + HMAC_SHA256(scope_key, "agent-project:" + normalized_workdir).hex()[:32]
```

Here `agents.id` is the Turn's claim-time-validated immutable
`executing_agent_id` snapshot, never a later read of the Session route or Agent
catalog. It deliberately survives hard deletion without constraining the Agent
row.

The raw Agent id, Agent name, workdir, Session id, Turn id, Run id, platform
identity, and user principal are never structural Memory columns, owner/filter
values, or provider path components. The source Turn id is reduced to a keyed
digest for idempotency. Because v1 deliberately preserves exact backend texts,
those bytes may still contain user/Agent-authored identifiers or the disclosed
Avibe-injected attribution lines. They remain bounded untrusted content and are
scrubbed with the payload; no content parsing may promote them into identity or
scope. The synthetic input actor is `i-` plus the first 32 hex characters of a
separate HMAC domain over that source digest; it can never collide with `u-` or
`a-` owners.

One binding maps one `binding_key` to one project id. Project ids use the schema
v3 write contract exactly: literal `default`, or a 1-63 byte lower-case named
slug accepted by `is_new_stored_memory_project_id`; reserved values and legacy
`p-...` workdir hashes are never new writes. `agent_id` partitions Agents and
`project_id` partitions one Agent's learning. Search/list responses must match
both before they cross the provider boundary.

The owner UI derives one aggregate navigation/credential guard,
`memory_roles_enabled = memory.enabled || memory.agent_track.enabled`. The
Memory page remains visible and its recovery controls remain reachable whenever
either role is enabled or a Memory recovery intent is pending, including the
valid Agent-only state where Personal Memory is disabled. An explicit shared
endpoint credential clear is accepted only when both roles will be disabled and
no pending recovery requires that credential. Personal-only checks must never
hide Agent status or authorize credential removal from an active Agent role.

### Explicit retrieval

The CLI adds explicit Agent Memory operations under `vibe memory agent`:

```text
vibe memory agent search <query> [--project <slug>] [--kind case|skill|all] [--limit 1..20] [--json]
vibe memory agent list [--project <slug>] [--kind case|skill] [--page N] [--limit 1..20] [--json]
```

The CLI's session-stable environment provides only an Avibe Session locator; it
is never sufficient authorization and carries no Turn id. Immediately before a
recognized `vibe memory agent` tool invocation, an Avibe-owned backend callback
asks the controller to resolve exactly one native-started, nonterminal active
Turn for that Session and mint a random one-use capability bound to the Session,
logical/native Turn, executing Agent snapshot, closed search/list command class,
parser-normalized argv/request digest, and a 30-second expiry. The callback
injects it only into that child invocation. The recognizer uses a shell parser
and accepts one standalone simple command with allowlisted CLI flags; shell
operators, pipelines, substitutions, redirections, environment wrappers, and
extra commands receive no capability. The CLI sends the token over the
authenticated controller UDS, where it is atomically
consumed before the controller performs the bounded retrieval. Tokens are kept
only as hashes in controller memory, expose no raw ids, and are invalidated by
controller restart.

For a cached Claude SDK client, the per-tool permission/hook callback is a
session-bound closure and uses the SDK's `updated_input` result to inject a fresh
capability for each matching Bash call. It does not rely on or refresh the
client's spawn-time environment. Codex and OpenCode use the equivalent
per-invocation adapter hook. If a backend cannot provide the hook, the token is
missing/expired/reused, or active Turn resolution is zero/ambiguous/terminal,
the command returns `memory_turn_context_unavailable`; there is no fallback to a
static Session lookup, caller-supplied Turn id, prompt text, or mutable Session
routing. Explicit Harness/CLI Agent overrides are already reflected in the
capability's immutable executing-Agent snapshot. The command also accepts no raw
`agent_id`, Agent selector, workdir, provider filter, or cross-project `all`.
Omitting `--project` uses the Session workdir's current explicit binding. An
explicit value must be either that binding or an exact project retained in this
executing Agent's read-only project catalog; without either, access fails closed.
The owner Settings UI may select an installed Agent or a read-only deleted-Agent
tombstone and one exact current or cataloged historical project. A deleted
selector is synthesized from the opaque project catalog and labelled `Deleted
Agent <opaque-suffix>`; it exposes neither the former raw Agent id nor a
caller-controlled provider filter. The CLI remains bound to its executing Turn,
so deleted-Agent retrieval is an owner-UI-only operation. Both surfaces search
and list `agent_case` and `agent_skill` with the same per-kind and response-size
bounds.

Results are provider-neutral records labelled `case` or `skill`. Every skill in
human-readable, JSON, and UI search/list output includes its `updated_at` as a
UTC RFC 3339 timestamp and its numeric `maturity_score` in the closed `[0, 1]`
range. EverOS 1.2.3 omits `updated_at` from its public Agent retrieval shapes, so
the Avibe-owned agent sidecar enriches the validated response through a
read-only, sentinel-confined lookup of the exact `agent_skill` row in that role's
provider root. Missing, malformed, or mismatched metadata invalidates the
response rather than displaying a guessed timestamp or a partial result.

Returned skill content is untrusted inert text. Avibe does not copy a returned
`SKILL.md`, load its references or scripts, register it with an Agent backend,
execute it, or add it to any system/user prompt. The existing Personal Memory
prompt and `vibe memory search/list` contracts remain byte-for-byte unchanged by
the new CLI examples and parser registration.

Memory status also reports a content-free skill-count observation for each
installed or cataloged deleted Agent across that Agent's current and cataloged
historical projects. An explicit status refresh may advance one durable,
stable-order sampling cursor at most
once per 60 seconds. One sampler batch issues at most 32 scoped `total_count`
requests with concurrency at most two, caches only count/observed-at metadata,
and resumes at the next Agent/project pair; status rendering never fans out its
own provider reads. An Agent count is complete only after all of its current
retrievable projects have been observed in the same sampling generation;
otherwise it is `unknown` or explicitly stale. Hard deletion relabels but does
not invalidate a cataloged Agent's cache; binding removal likewise retains
observations through the project catalog entry.

A complete count reports `normal` for 0-7, `approaching` for 8-10, and `risk`
above 10. The hint says explicitly that this total is a conservative proxy:
EverOS does not expose cluster membership, name sanitization collisions can
occur at any count, and the stale-index risk applies when one upstream cluster
exceeds 10. An unavailable count degrades only Agent Memory status. Counts and
hints never block capture, retrieval, or provider writes.

## Isolation Architecture

### Owned state

The two provider roles use disjoint paths under the effective `AVIBE_HOME`:

| Role | Provider root | Private UDS | Generated config |
|---|---|---|---|
| Personal Memory | `memory/everos-root/` | `memory/.rt/everos.sock` | `memory/generated/chat/` after role migration |
| Agent Memory | `memory/everos-agent-root/` | `memory/.rt/everos-agent.sock` | `memory/generated/agent/` |

The existing generated files may be migrated into the `chat/` role directory
only through a compatibility-preserving write; the sentinel-owned provider root
and socket identity do not change. Every generated configuration pins
`memorize.mode` explicitly: `chat` for the existing root and `agent` for the new
root. Validation accepts only the role's expected value. EverOS's upstream
default of `agent` is never relied upon.

The roles share the immutable installed EverOS artifact and environment-only
endpoint credentials. Consequently the remote LLM/embedding service's account
quota, concurrency allowance, and rate limits are an explicit external resource
coupling: Agent traffic can throttle Personal traffic even though failures and
state remain role-local. Both roots and `memory.sqlite` also share the effective
home volume, so local capacity is an explicit resource coupling: growth of the
Agent EverOS root or index can consume the reserve and make later Agent or
Personal writes fail even though paths are disjoint. The pre-claim free-space
guard limits new Agent work but cannot quota or preempt growth caused by work
already accepted inside EverOS. Status therefore reports free space and
role-specific provider-root use, warns at the Agent reserve, and pauses new
Agent claims while below it; this is observability and backpressure, not local
capacity isolation. The roles do not share provider roots, sockets, process
records, root sentinels, provider-root locks, supervisors, health snapshots,
call-log ownership slots, or worker queues. All ownership records include the
role plus exact root/socket pair so a stale chat record can never identify an
agent child, or vice versa.

The agent sidecar reuses EverOS's public `GET /health` and POST
`/api/v2/memory/{add,flush,search,get}` paths. Role-specific validators, not new
provider paths, define the wire contract. The chat sidecar retains its exact
current profiles and continues rejecting every agent shape.

### Lifecycle and maintenance

Reconcile owns the roles independently:

- Personal Memory enablement continues to own the chat root and user worker.
- Agent-track enablement owns the agent root, scanner, trajectory worker, and
  agent sidecar. Turning it off preserves its queue and provider data.
- A crash, failed health check, recorder degradation, or processing error in one
  role changes only that role's projection and retry loop.
- Agent `/add` failures cannot merge into or alter a chat-root `/add` result.
- Shutdown closes both owned child trees independently under the normal
  controller lifecycle; no routine verification restarts Avibe.

Restart engine, Repair index, Clear Memory Data, rebuild, and factory reset must
enumerate role-owned resources rather than assuming one global slot. Either
never-enabled role whose root, sentinel, process record, and socket are all
absent is a successful `absent` no-op. A present role path without its expected
sentinel remains unsafe and fails closed. Clear and factory reset cover every
existing owned root, both role call-log/health records, and both queues under one
existing durable maintenance fence while preserving the V2 config binding
source. A partial failure reports each role truthfully and keeps Memory fenced
until idempotent retry converges.
Embedding-identity rebuild validates once, quiesces both sidecars, rebuilds each
nonempty owned root independently, and does not activate either root against a
different vector-space identity. Restart/repair may target a degraded role
without mode-flipping or deleting the healthy role.

## Schema-v4 Contract

Raise `MEMORY_STORE_SCHEMA_VERSION` from 3 to 4 with a transactional v3-to-v4
migration. A released schema-v3 fixture must upgrade with every existing row,
table, index, value, and project-catalog entry unchanged. The new tables are
separate from `memory_capture_queue` and its user principal/session-generation
invariants.

### Commit-ordered terminal source

The infrastructure slice adds these nullable columns to `session_turns` in the
primary Avibe state schema:

- `executing_agent_id TEXT`, an indexed immutable copy of the stable Agent id
  selected by the final execution route, deliberately without a foreign key;
- `source_class TEXT`, constrained on new writes to `interactive`,
  `harness_request`, `callback`, `maintenance`, or `agent_callback`;
- `backend_input_shape TEXT`, constrained on new writes to `text_only` or
  `out_of_band_attachment` and derived from the final native request rather
  than transport transcript metadata;
- `backend_dispatch_binding_key TEXT`, the opaque start-snapshot key for the
  committed explicit source-workdir binding; it is present only with an admitted
  dispatch and contains neither workdir nor project id;
- `backend_dispatch_binding_epoch INTEGER`, the immutable monotonic generation
  returned with that key; the pair must resolve through one continuous project
  epoch at terminal settlement;
- `backend_dispatch_text TEXT` and `backend_dispatch_sha256 TEXT`, the exact
  bounded final backend-facing input and digest;
- `backend_dispatch_capture_epoch INTEGER` and
  `backend_dispatch_omission_reason TEXT`, the admitted epoch or a closed
  `disabled | transition | unbound | out_of_band_attachment | oversized |
  budget | binding_cutover | destructive_reset` reason, never both; and
- `terminal_sequence INTEGER`, with a unique partial index.

The shared `MessageHandler`/dispatch boundary creates the same durable Delivery
and Turn owner for Workbench and every supported IM execution before native
dispatch. IM adapters continue to provide transport context and transcript
mirroring, but no adapter-specific mirror row is a Memory source.
`persist_silent_terminal` remains activity evidence rather than a trajectory;
the shared owner now also settles the corresponding IM Turn. Harness uses its
existing durable path.

The initial Delivery claim/Turn creation transaction snapshots
`executing_agent_id` from the resolved execution route, including an explicit
Harness/task override, after validating that id against the live Agent catalog.
It also snapshots the homogeneous Delivery `source_class`. These fields are
observational for Memory: inability to resolve a stable Agent id/source class
does not block the Turn, but leaves the field `NULL` and makes that Turn
ineligible for Agent Memory. The Agent snapshot is not referentially constrained,
so existing `VibeAgentStore.remove()` hard deletion remains valid; the already
validated id string survives only as immutable execution history and HMAC input.
Retries preserve the first snapshots, and no later Session/catalog update may
rewrite them.

The same primary migration adds nullable `message_deliveries.source_class` with
the same closed values. Reservation derives it from durable provenance before a
Delivery can queue; batching compatibility and Turn claim use the column rather
than reparsing later mutable metadata. Pre-migration/missing classes remain
fail-closed for Agent Memory but do not block ordinary execution.

The primary schema also keeps a singleton snapshot-byte reservation capped at
128 MiB. Immediately before the first native start call, after every
shared/backend input transformation, a primary state transaction classifies the
complete native input shape, checks the committed capture epoch, resolves one
committed explicit opaque binding key, enforces the per-row 256 KiB bound,
reserves bytes, and compares-and-sets the admitted
text/digest/shape/binding-key/binding-epoch/capture-epoch. The adapter receives
that exact stored text without additional native input. A retry must reuse it and
a conflicting second value fails closed; Memory never derives candidate input
from the earlier raw `dispatch_text`. When no snapshot is admitted, the same
transaction writes only the closed shape/omission reason and the adapter
proceeds with its in-memory native input. Unbound content and out-of-band
attachment metadata/bytes are
never copied into the Memory source schema or charged to its byte reservation.
Startup and periodic primary-state maintenance recompute the reservation from
the UTF-8 byte lengths of actual nonempty snapshots before admitting more, so a
crash cannot leak budget.

The transaction that first settles a Turn terminal assigns one greater than the
maximum non-NULL `terminal_sequence` (or `1` when none exists) while holding
SQLite's serialized writer lock. The assignment, terminal outcome/evidence,
and terminal state commit together; retries cannot allocate a second value.
Consequently sequence order is settlement commit order even when wall time
moves backward or UUIDs sort in another order. `terminal_at` remains display
metadata and is never a scanner cursor. Pre-migration Turn rows retain `NULL`
for every new field and are not backfilled.

Terminal settlement in a disabled or different capture epoch clears any
snapshot and releases its byte reservation in the same primary transaction.
For an eligible terminal Turn, the scanner clears the primary snapshot and
releases the reservation after its enqueue, duplicate, guarded decline, or
closed skip is durable. A crash between the Memory-store decision and that
cleanup is repaired idempotently by comparing terminal sequence with the durable
scanner cursor. Abandoned-Turn recovery uses the same cleanup. Clear and
Reinitialize/factory reset first close new dispatch snapshot admission. After
destructive deletion, the same serialized primary transaction that records the
new epoch and terminal high water bulk-scrubs every admitted snapshot, including
nonterminal/in-flight Turns without a `terminal_sequence`, releases their byte
reservations, and replaces their admitted capture/binding epochs and key with
the closed `destructive_reset` omission reason. Turn settlement cannot
interleave with that transaction; a later settlement is therefore epoch-crossing
and ineligible.
The destructive operation cannot report success or recreate scan state before
this scrub commits. Thus the primary duplicate is bounded while live and does
not survive final disposition or destructive reset, even when native execution
remains in flight.

The primary state schema also adds bounded singleton
`memory_agent_capture_cutover` and `memory_agent_binding_cutover` recovery
records used below. Each contains its kind, prior/desired Agent-block config
digests, `prior_memory_config_revision`, `desired_memory_config_revision`,
terminal high water, closed `prepared | committed` phase, and timestamps. The
capture record also stores its epoch and desired state. The binding record stores
its bounded desired opaque key/generation set but no normalized workdir/project
payload; those remain in V2 config, while opaque project epochs remain in the
Memory store. V2 config persists one monotonic
`memory.revision` incremented exactly once by each successful controller Memory
mutation. Recovery requires both revision and digest to match the durable prior
or desired pair, so an ABA edit cannot masquerade as either state. A released
config without the internal revision loads as revision `0`; migration preserves
all other values, and the first successful controller mutation writes `1`.

The primary schema also adds
`memory_agent_capture_projection(capture_epoch, enabled,
memory_config_revision, applied_agent_config_digest, updated_at)` as the durable
singleton read by native start. The transaction admits a snapshot only when this
projection is enabled, its revision matches the binding projection, and no
capture/binding/maintenance intent exists. Enable, disable, and destructive
reset replace it in the same primary transaction that clears the corresponding
intent, after the role-specific work has converged. Startup validates it against
V2 config and an exact pending intent; missing or mismatched state fences
admission as `transition` and never derives a fresh epoch from wall time or the
Memory store. Every successful Memory config transaction, including a
Personal-only patch, stamps both current Agent projections with the new config
revision before releasing the controller lock; when Agent semantics are
unchanged it preserves their epoch/generations. Thus equal projection revisions
cannot both lag the persisted config after a completed writer.
Controller startup keeps native snapshot admission globally fenced until this
validation and Agent-role runtime reconciliation complete; a failed role restart
retains enabled config but enters the existing retryable recovery state instead
of trusting the old projection alone.

The primary schema also adds `memory_agent_admission_bindings(binding_key,
binding_epoch, memory_config_revision)` as the content-free current binding set
used only by the dispatch gate. A binding cutover intent fences reads as
ineligible, then the controller atomically replaces this opaque set and promotes
the intent after V2 config persistence. Every addition or reassignment allocates
a new monotonic `binding_epoch`; re-adding the same workdir never reuses a closed
generation. Recovery rebuilds the set only from an exact desired
revision/digest pair and the intent's allocated generations. It stores neither
project ids nor normalized workdirs and is never the scanner's historical
project authority.
`memory_agent_binding_epoch_counter` is a primary singleton incremented in the
prepared-intent transaction for every added or reassigned key. Aborted cutovers
may leave gaps but never reuse a generation, even after closed historical epochs
are compacted.

### Agent trajectory outbox

`memory_agent_trajectory_queue` stores one row per admitted source Turn:

- `source_turn_digest` primary key: keyed digest of the durable Turn id;
- stable provider `session_id`, opaque `agent_id`, and exact `project_id`;
- bounded input/result payloads and stable input/result timestamps;
- closed `pending | processing | awaiting_flush | delivered | dead` state;
- lease token/owner/time, attempts, next retry, closed error, provider request
  ids/status, durable add receipt/flush requirement, and created/completed
  timestamps.

Before enqueue, the store enforces an independent maximum of 500 nonterminal
Agent rows and at least 512 MiB free on the volume containing `memory.sqlite`.
At either guard, the scanner durably declines the otherwise semantically
eligible source: it records an aggregate
`missed_queue_full`/`missed_low_disk_space` count and advances past that source
sequence in the same transaction; it never retains the rejected trajectory
text. These guards are independent of, and do not consume, Personal Memory's
500-row allowance. Status reports the counters without source content.

Idempotent enqueue and cursor advancement are one transaction. A duplicate
source digest returns duplicate without changing the existing row. Immediately
after a confirmed add, one queue transaction persists the sanitized add receipt
and exact session/app/project flush scope, scrubs both source texts, and moves
the row directly to `delivered` when no flush is required or to
`awaiting_flush` before any flush attempt. Flush and every flush retry use only
that scope; provider/infrastructure unavailability can therefore never retain
trajectory text. A deterministic add rejection, exhausted bounded add retry, or
unknown post-submission add outcome dead-letters and scrubs the row. Agent rows
never enter `manual_required` or the Personal Memory session-flush coordinator.

Scrubbed `delivered` and `dead` rows are idempotency tombstones with the same
bounded retention as Personal Memory: retain at most 90 days and, within that
window, only the newest 100,000 closed Agent rows. Transactional compaction runs
after settlement and from periodic store maintenance, deletes oldest excess
tombstones, and never changes the scan cursor, enable/drain state, or aggregate
missed counters.

Each row uses its own deterministic provider session. The worker performs one
two-message add and, when needed, one matching flush. A confirmed add receipt
means only that EverOS accepted the trajectory boundary and authorizes immediate
payload scrubbing; a successful flush/terminal acknowledgement still does not
prove a case or skill exists. Unknown post-submission add outcomes are never
replayed automatically; they settle to a scrubbed agent-only dead-letter reason
so an ambiguous agent write cannot fence user capture.

### Scanner cursor and project bindings

`memory_agent_scan_state` is a singleton containing the last committed terminal
sequence, applied capture epoch/state, optional disable drain-through sequence,
durable missed counters, and scan/update timestamps. Enable and destructive
reset projection initialize the cursor from the exact primary capture intent
high water, never from a fresh cross-database read. Disable projects that same
intent high water as its inclusive drain target. The scanner queries strictly
after its cursor in bounded pages and may cross an epoch boundary only according
to the primary cutover record. It advances through admitted, duplicate, guarded,
and closed-skip rows only after the corresponding decision is durable. A crash
before commit re-reads the same row; the digest keeps enqueue idempotent.

`memory_agent_project_bindings` is an opaque, sequence-versioned projection of
the V2 config. Each epoch stores `binding_key`, its monotonic `binding_epoch`,
exact new-style `project_id`, an exclusive `effective_after_sequence`, an
optional inclusive
`effective_through_sequence`, and created/closed timestamps. The V2 config
remains the authoritative desired owner setting. The projection also stores its
`applied_config_digest`; historical epochs exist only to finish committed
scanner work.

`memory_agent_project_catalog` is a conservative read-only retrieval directory
keyed by opaque `agent_id` plus exact `project_id`. The enqueue transaction
creates or refreshes an entry before provider data can exist. Binding removal and
closed-epoch compaction never delete it, so later-delivered queued output and
existing provider cases/skills remain discoverable after the last binding is
removed or reassigned. Entries contain no workdir or raw Agent id and disappear
only with the Agent root's Clear/factory reset, or a future explicit
provider-data deletion contract. A catalog entry does not claim that EverOS
produced output.

Installed Agents are joined to catalog owners by recomputing their opaque
`agent_id`. A catalog owner with no live join remains a read-only deleted-Agent
tombstone instead of becoming unreachable. Owner Settings issues a bounded
opaque selection token for that exact catalog owner; requests never accept a raw
`agent_id`. Hard deletion therefore remains unconstrained and does not delete
or orphan queued/provider data.

The shared controller `apply_memory_settings` transaction owns every binding
add, reassignment, and removal under the cross-process lifecycle/config lock;
its Agent-role operation is `apply_agent_memory_settings`. It first uses one
serialized primary state transaction to read the current committed
terminal high water `H` and insert a prepared singleton
`memory_agent_binding_cutover` intent containing the prior/desired Agent-block
digests, whole-config revisions, bounded desired opaque key/generation set, and
`H`; additions/reassignments allocate that set from the durable generation
counter in this transaction. Terminal settlement cannot interleave
between that read and intent commit. The scanner is then fenced, V2 config is
atomically replaced. On the exact desired revision/digest, one primary
transaction promotes the intent, replaces the current admission set, and scrubs
every old-generation snapshot whose Turn is still nonterminal or settled after
`H`, releasing its reservation and recording `binding_cutover`. A terminal Turn
at or below `H` is already old-epoch backlog and is not scrubbed. Terminal
settlement cannot interleave with this promotion/scrub transaction. Thus removal
or reassignment invalidates every in-flight old generation before a re-add can
open a new one, while a failed config write cancels without destroying a valid
snapshot. Then the historical project projection converges; terminal settlement
performs no Memory/config I/O and never waits for projection work. The UI does
not write these bindings before contacting the controller.

When one Settings save changes both `enabled` and bindings, that same primary
writer transaction records the capture and binding intents at one shared `H`.
Binding epochs project before an enabled scanner may open; a disabled drain uses
the old binding epochs through `H`.

While an intent exists, Agent scanner admission is fenced. One idempotent Memory
transaction appends/closes the exact opaque key/generation epochs at the
intent's `H` and advances `applied_config_digest`; only then may recovery clear
the primary-state intent and reopen the scanner. The binding becomes
sequence-effective at the
prepared intent's `H` only if the desired config commit succeeds, and the
Settings save reports success only after projection converges. A Turn at or
below `H` uses the old mapping; a later Turn uses the new mapping after a
successful save, or the old mapping after a failed save cancels the intent.

If config persistence fails, the prior revision/digest pair cancels the prepared
intent and leaves the projection unchanged. After a crash, the prior pair
cancels it, the desired pair promotes it and reuses the exact original `H`, and
any third or ABA state leaves only Agent Memory fenced and degraded. If
projection already committed, matching revisions and digests make intent cleanup
idempotent. Recovery never creates a cutover at restart time. A crash therefore
cannot backdate the new binding, extend a disabled epoch, or block the Agent hot
path.

An added mapping is effective only for `terminal_sequence > H`. Reassignment
closes the old epoch through `H` and opens the new project after `H`; removal
only closes the old epoch through `H`. The scanner resolves each source's
immutable start-snapshot binding key/generation against the unique row satisfying
`row.binding_key = snapshot.binding_key`,
`row.binding_epoch = snapshot.binding_epoch`, and
`effective_after_sequence < terminal_sequence <= effective_through_sequence`,
with a `NULL` through-sequence meaning open. It therefore drains backlog through
the old project without admitting opt-in or reassignment history to the new
project. Closed epochs remain until the committed scanner cursor reaches their
cutover, then compact; immutable queued/provider rows keep their original
project and the read-only catalog keeps that project retrievable.

Settings writes friendly normalized workdir/project pairs only to V2 config;
reconcile derives opaque keys with the current Memory scope key. Reads expose
the current config labels, never by reversing an opaque key. Clear may remove
all epochs; factory reset creates a new Memory scope key and rebuilds current
bindings with an effective-after cutover at the new high water before an enabled
scanner starts.

## Provider and Sidecar Contracts

The provider-neutral port adds four operations:

- `add_agent_trajectory(trajectory)`;
- `flush_agent_trajectory(ref)`;
- `search_agent_memory(agent_id, project_id, query, kind, limit)`; and
- `list_agent_memory(agent_id, project_id, kind, page, page_size)`.

The agent sidecar accepts only:

- add: exact scope plus exactly two text-only messages, first synthetic user
  actor and second assistant owner `a-[0-9a-f]{32}`;
- flush: the exact session/app/project scope from an admitted trajectory;
- search: `agent_id` only, `include_profile=false`, no arbitrary filters, and
  bounded query/top-k. EverOS returns both Agent arrays; the adapter validates
  both completely before applying Avibe's requested case/skill projection; and
- get: `agent_id` only, `memory_type=agent_case|agent_skill`, 1-based bounded
  paging, and fixed allowlisted ordering. Skill responses are joined read-only
  to the exact root-confined `agent_skill` metadata for `updated_at`; list
  `total_count` also feeds the content-free status monitor.

Both `user_id` plus `agent_id`, raw Avibe ids, `u-...` owners, mismatched message
owners, legacy/reserved projects, unknown keys, attachments, tool payloads,
filters, and cross-role requests are rejected before forwarding. Responses are
fully validated for envelope, owner, app/project, item kind, pagination/counts,
closed numeric ranges, and bounded canonical text. A malformed or cross-scope
response becomes `memory_provider_response_invalid` with no partial projection.

Agent add/flush remain inside the mandatory redacted recorder boundary. Insight
patches, recorder, and reader recognize agent-pipeline events but never expose
trajectory text, raw Agent/Turn/Session ids, provider paths, prompts, model
responses, or credentials. Agent recorder failure degrades only the agent role.

## Failure and Degradation

- Scanning, enqueue, add, flush, and retrieval are outside Agent hot paths.
- A malformed agent config, missing binding, missing immutable Agent id,
  unavailable sidecar, provider error, or queue problem produces one sanitized
  agent-track status/skip/failure and leaves the completed Turn unchanged.
- Queue work uses bounded backoff and agent-only dead letters. A poisoned row
  cannot block later rows or the Personal Memory worker.
- The chat root stays available when the agent root is down, and user capture
  remains governed exclusively by the existing human-input admission.
- These isolation guarantees cover ordinary scanning, processing, retrieval,
  and role-reconcile failures in Avibe-owned state and processes. The shared
  remote processing credentials do not provide quota isolation: Agent load can
  consume account concurrency/rate/quota and throttle Personal requests. Agent
  work remains bounded and backs off on throttling, but Avibe cannot partition
  an endpoint provider's account budget. The shared effective-home volume also
  does not provide capacity isolation: post-admission EverOS Agent-root/index
  growth can cross the free-space reserve and make later Personal or Agent local
  writes fail. Role-specific paths still prevent state mixing or logical
  corruption, but status must report this capacity coupling and new Agent claims
  pause below the reserve. Explicit Clear, factory reset, and
  embedding-identity rebuild intentionally use the shared maintenance fence and
  can pause both roles until their idempotent operation converges.
- Retrieval failure returns a closed Agent Memory error and never falls back to
  Personal Memory or a different Agent/project.
- Empty case/skill results after successful processing are valid. UI and CLI say
  `processed`/`no results`, never `learned`, until retrieval observes output.

## Accepted EverOS 1.2.3 Limitations

These are known by design and are not fixed in this issue:

- agent-skill retire is unimplemented, so retired output may remain searchable;
- skill names are sanitized for filesystem paths and collisions can overwrite an
  earlier `SKILL.md`, including case-only collisions on insensitive filesystems;
- clustering above `MAX_SKILLS_IN_PROMPT=10` can use a stale partial index and
  clobber a concurrent/newer skill view;
- provider quality gates can legitimately emit a case but no skill, or no
  durable Agent output for an accepted trajectory;
- 1.2.3 is the first release with the relevant cascade-race fix; earlier
  behavior is not supported by this feature; and
- the markdown shape supports `references/` and `scripts/`, but the 1.2.3
  extraction path populates only `SKILL.md`.

The dedicated agent root ensures these limitations cannot corrupt Personal
Memory state. Bounded validation and retrieval ensure malformed or oversized
Agent output degrades closed instead of crossing into the user track. The
freshness/maturity fields and count hint above are observability mitigations
only: they help an owner judge possible staleness but do not retire, rename,
repair, or rewrite any skill.

## Delivery Slices

1. **Spec:** this contract plus the canonical product/recovery documentation.
   No runtime behavior changes.
2. **Infrastructure:** schema v4, config/identity/project-binding primitives,
   platform-neutral Workbench/IM/Harness Turn ownership, dual-root lifecycle,
   provider port, role-specific sidecar/recorder contracts, and focused
   migrations/fixtures. Capture remains inactive.
3. **Capture:** durable scanner/read port, admission, enqueue, worker/delivery,
   lifecycle composition, and degradation tests.
4. **Retrieval:** scoped CLI/internal API, owner Settings enablement/binding,
   independent status with per-Agent skill-count hints, freshness/maturity
   enriched Agent Memory search/list UI, i18n, and prompt-inert parser contracts.
5. **Scenario closed loop:** catalog, reusable harness, packaged-runtime
   regression, final docs/observations, and issue close-out evidence.

No PR is stacked. Each slice branches from the latest `master` after the prior
slice is approved and merged.

## Scenario Contract

The final catalog is `tests/scenarios/memory_agent_track/` and registers these
stable ids:

| ID | Invariant |
|---|---|
| `MEMORY-AGENT-001` | The absent/default config leaves the second root, scanner, and worker off; every Personal/Agent/endpoint Memory config writer uses one controller transaction with monotonic revision/digest conflict detection through cutover cleanup; the durable current-capture projection preserves exact enabled epoch admission across controller restart; Agent-only enablement keeps navigation/recovery visible and forbids shared-credential clearing; a failed Agent-role start retains a fenced retryable intent without snapshot admission; destructive reset atomically scrubs terminal and in-flight snapshots at its fixed high water; and Clear accepts either never-created role as absent. |
| `MEMORY-AGENT-002` | Every semantically eligible non-steered completed Workbench, Slack, Discord, Telegram, Feishu/Lark, WeChat, or Harness Turn not durably declined by a specified snapshot/queue/disk guard is represented once by its exact post-transformation backend-dispatch/result pair. |
| `MEMORY-AGENT-003` | Admission excludes every terminal shape that does not satisfy the completed-result invariant; source-class batching separates callbacks; accepted live steers are excluded; the revision-matched opaque primary admission set makes an unbound or binding-transition Turn retain no dispatch text or budget; a remove/reassign scrubs old-generation Turns settling after its high water so re-add cannot revive them; and any final native request with an out-of-band attachment fails closed without retaining attachment metadata or bytes. |
| `MEMORY-AGENT-004` | Commit ordering and crash/replay cannot lose or enqueue one source Turn more than once; persisted prior/desired config revisions plus digests detect third-state and ABA edits during capture or binding recovery. |
| `MEMORY-AGENT-005` | Agent and project partitions cannot read or write each other's output; capture and controller-resolved active-Turn CLI retrieval use the immutable executing Agent without blocking hard deletion; deleted owners remain UI-retrievable; and crash-safe generation-bearing binding cutovers preserve settled backlog while invalidating in-flight old generations. |
| `MEMORY-AGENT-006` | Malformed config, missing/pre-migration identity/source/final-dispatch snapshots, and missing project binding fail closed. |
| `MEMORY-AGENT-007` | Agent-sidecar/local pipeline outage leaves chat and Personal Memory healthy; dual-root embedding rebuild preserves and fences both roots through partial failure; shared remote endpoint throttling and effective-home capacity exhaustion are reported as explicit resource couplings without cross-role state mixing. |
| `MEMORY-AGENT-008` | Both role sidecars reject every payload outside their exact owner/shape contract. |
| `MEMORY-AGENT-009` | CLI/UI retrieval is explicit, bounded, inert, and absent from Agent prompts/install paths; each adapter injects a request-bound one-use active-Turn capability, cached Claude sessions refresh it through per-call `updated_input` without an environment Turn id, and missing/reused/expired/ambiguous/terminal context fails closed; a read-only per-Agent project catalog keeps removed-binding output reachable. |
| `MEMORY-AGENT-010` | Accepted processing with zero cases or skills is a truthful valid outcome. |
| `MEMORY-AGENT-011` | Per-row/global primary snapshot guards plus queue/disk exhaustion skip durably; primary copies, including active nonterminal snapshots, are scrubbed after disposition/reset; queue payloads are scrubbed immediately after a durable add receipt and before flush; ambiguous post-submission Agent adds dead-letter without replay; and 90-day/100,000-row tombstone compaction bounds closed-row storage without degrading Personal Memory. |
| `MEMORY-AGENT-012` | Skill retrieval exposes exact freshness/maturity metadata, while the request-budgeted status sampler reports non-blocking per-Agent count hints at 8 and 11 skills. |
| `MEMORY-AGENT-013` | Opt-in disclosure models raw attribution bytes in exact trajectory content without allowing them to become Memory identity or scope. |

Evidence layers are unit tests for config/identity/admission/store/worker/runtime,
contract tests for provider/sidecar/internal API/CLI/UI, executable catalog
scenarios for the closed loop, and one hermetic packaged EverOS regression. No
test may read or write real `~/.avibe`, restart the local `vibe` service, or use
external provider credentials.
