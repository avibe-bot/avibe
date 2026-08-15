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
   Harness-specific `agent_runs`. Both interactive and Harness turns are
   eligible under the same rules.
3. A controller-owned background scanner copies eligible terminal turns into a
   separate schema-v4 queue. Terminal settlement and backend adapters do no
   Memory I/O and await no Memory work.
4. EverOS `agent_id` is derived from immutable `agents.id` with the install-local
   Memory scope key. Mutable Agent names and user principals are never agent
   owners.
5. Agent/project retrieval is explicit, lazy, inert, and bounded. Agent Memory
   is never injected into a prompt, installed as a Codex/Claude skill, or
   executed.
6. The first version contains only the exact backend dispatch text and terminal
   result. It does not parse formatted event streams into synthetic tool calls.
7. Enabling the track does not create a project binding. The owner must bind a
   source workdir to either the exact `default` project or an existing/new-style
   named Memory project. Missing bindings fail closed.
8. EverOS 1.2.3 limitations listed below are accepted. Avibe isolates and
   reports them but does not patch EverOS or file an upstream issue.
9. Every disabled-to-enabled transition initializes the scan cursor to the
   current terminal-settlement sequence high water mark. There is no historical
   or disabled-period backfill in v1; only turns settling after that explicit
   enable cutover are candidates.

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
Turns, including interactive and Harness turns; v1 has no source-type selector.

Enabling requires the installed Memory runtime, complete processing endpoints,
and at least one explicit project binding. Under the agent-capture lifecycle
lock, every disabled-to-enabled operation snapshots the current committed
terminal-sequence high water, persists it as the new scan cursor, rebuilds the
opaque binding projection, and only then opens scanner admission. A disable
closes scanner admission and waits for the current bounded scan transaction
before it reports success. It preserves rows admitted before that cutover but
never later imports turns settled while disabled. A failed start leaves the
agent-track projection unavailable without disabling Personal Memory or
restarting the Avibe service.

### Admission

One durable `session_turns` row is one candidate trajectory. The scanner reads
in stable `terminal_sequence` order and joins the owning `agent_sessions` row
and its referenced `agents` row. Admission is provider-neutral and accepts a
row only when all of these invariants hold:

- the turn is terminal with `terminal_outcome="completed"`;
- `terminal_evidence_kind="terminal_result"` and the versioned evidence contains
  one non-empty final `result_text`;
- immutable `dispatch_text` and the final result are valid, nonempty UTF-8 text
  of at most 256 KiB each;
- the session's `agent_id` resolves to a real `agents.id`; legacy name-only
  sessions fail closed, while later Agent disable/archive does not rewrite an
  already completed Turn's identity;
- the source is an ordinary interactive or Harness-request turn, not a callback,
  system maintenance action, or Agent-to-Agent completion callback; and
- the session workdir resolves through an explicit opaque binding to exactly one
  new-style Memory project.

The property is deliberately positive. Every terminal shape not satisfying the
complete invariant is skipped, including failed, canceled, stopped/restarted,
silent, missing-result, malformed-evidence, oversized, and unbound turns.
Admission logs only a closed reason and opaque source digest.

The v1 trajectory has exactly two ordered messages:

```json
[
  {
    "sender_id": "i-<32 lowercase hex>",
    "role": "user",
    "timestamp": 0,
    "content": "<exact dispatch_text>"
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

The raw Agent id, Agent name, workdir, Session id, Turn id, Run id, platform
identity, and user principal are not written into the Memory database or
provider paths. The source Turn id is reduced to a keyed digest for idempotency.
The synthetic input actor is `i-` plus the first 32 hex characters of a separate
HMAC domain over that source digest; it can never collide with `u-` or `a-`
owners.

One binding maps one `binding_key` to one project id. Project ids use the schema
v3 write contract exactly: literal `default`, or a 1-63 byte lower-case named
slug accepted by `is_new_stored_memory_project_id`; reserved values and legacy
`p-...` workdir hashes are never new writes. `agent_id` partitions Agents and
`project_id` partitions one Agent's learning. Search/list responses must match
both before they cross the provider boundary.

### Explicit retrieval

The CLI adds explicit Agent Memory operations under `vibe memory agent`:

```text
vibe memory agent search <query> [--project <slug>] [--kind case|skill|all] [--limit 1..20] [--json]
vibe memory agent list [--project <slug>] [--kind case|skill] [--page N] [--limit 1..20] [--json]
```

The CLI accepts an Avibe-injected Session id through the existing trusted
context, resolves that Session's immutable Agent and explicit project binding,
and accepts no raw `agent_id`, Agent selector, workdir, provider filter, or
cross-project `all`. Omitting `--project` uses that binding; an explicit value
must equal it. The owner Settings UI may select an installed Agent and one exact
bound project. It may search and list `agent_case` and `agent_skill` with the
same per-kind and response-size bounds.

Results are provider-neutral records labelled `case` or `skill`. Returned skill
content is untrusted inert text. Avibe does not copy a returned `SKILL.md`, load
its references or scripts, register it with an Agent backend, execute it, or add
it to any system/user prompt. The existing Personal Memory prompt and
`vibe memory search/list` contracts remain byte-for-byte unchanged by the new
CLI examples and parser registration.

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
endpoint credentials. They do not share provider roots, sockets, process
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
enumerate role-owned resources rather than assuming one global slot. A
never-enabled Agent role whose root, sentinel, process record, and socket are all
absent is a successful `absent` no-op. A present Agent path without its expected
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

The infrastructure slice also adds nullable
`session_turns.terminal_sequence INTEGER` plus a unique partial index in the
primary Avibe state schema. The transaction that first settles a Turn terminal
assigns one greater than the maximum non-NULL `terminal_sequence` (or `1` when
none exists) while holding SQLite's serialized writer lock. The assignment,
terminal outcome/evidence, and terminal state commit
together; retries cannot allocate a second value. Consequently sequence order
is settlement commit order even when wall time moves backward or UUIDs sort in
another order. `terminal_at` remains display metadata and is never a scanner
cursor. Pre-migration terminal rows retain `NULL` and are not backfilled.

### Agent trajectory outbox

`memory_agent_trajectory_queue` stores one row per admitted source Turn:

- `source_turn_digest` primary key: keyed digest of the durable Turn id;
- stable provider `session_id`, opaque `agent_id`, and exact `project_id`;
- bounded input/result payloads and stable input/result timestamps;
- closed `pending | processing | delivered | dead` state;
- lease token/owner/time, attempts, next retry, closed error, provider request
  ids/status, and created/completed timestamps.

Before enqueue, the store enforces an independent maximum of 500 nonterminal
Agent rows and at least 512 MiB free on the volume containing `memory.sqlite`.
At either guard, the scanner records a durable aggregate
`missed_queue_full`/`missed_low_disk_space` count and advances past that source
sequence in the same transaction; it never retains the rejected trajectory
text. These guards are independent of, and do not consume, Personal Memory's
500-row allowance. Status reports the counters without source content.

Idempotent enqueue and cursor advancement are one transaction. A duplicate
source digest returns duplicate without changing the existing row. Successful
delivery scrubs both source texts. A deterministic rejection or exhausted
bounded retry dead-letters and scrubs the row; infrastructure unavailability
pauses claims without consuming a content retry. Agent rows never enter
`manual_required` or the Personal Memory session-flush coordinator.

Each row uses its own deterministic provider session. The worker performs one
two-message add and, when needed, one matching flush. Acknowledgement means only
that EverOS accepted/processed the trajectory boundary; it does not prove a case
or skill exists. Unknown post-submission outcomes are never replayed
automatically; they settle to a closed agent-only dead-letter reason so an
ambiguous agent write cannot fence user capture.

### Scanner cursor and project bindings

`memory_agent_scan_state` is a singleton containing the last committed terminal
sequence, enable epoch, durable missed counters, and scan/update timestamps. On
every enable cutover the cursor advances to the primary store's current maximum
before admission opens. The scanner then queries strictly after that sequence in
bounded pages. It advances through admitted, duplicate, guarded, and closed-skip
rows only after the corresponding decision is durable. A crash before commit
re-reads the same row; the digest keeps enqueue idempotent. Adding a binding
later does not backfill turns already skipped at an earlier cursor.

`memory_agent_project_bindings` is a replaceable projection of the V2 config. It
maps only opaque `binding_key` to exact new-style `project_id`, with
created/updated timestamps. Settings writes the normalized workdir/project pair
to V2 config; reconcile derives the opaque key with the current Memory scope key
and transactionally replaces the projection. Reads expose the config's friendly
local workdir label, never by reversing the opaque Memory key. Clear may remove
the projection; factory reset creates a new Memory scope key and rebuilds every
opaque key from the preserved config before an enabled scanner starts. Deleting
a config binding stops new admission after reconcile and does not rewrite queued
or provider data already bound to that project.

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
  paging, and fixed allowlisted ordering.

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
- Retrieval failure returns a closed Agent Memory error and never falls back to
  Personal Memory or a different Agent/project.
- Empty case/skill results after successful processing are valid. UI and CLI say
  `processed`/`no results`, never `learned`, until retrieval observes output.

## Accepted EverOS 1.2.3 Limitations

These are known by design and are not Avibe bugs to work around in this issue:

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
Agent output degrades closed instead of crossing into the user track.

## Delivery Slices

1. **Spec:** this contract plus the canonical product/recovery documentation.
   No runtime behavior changes.
2. **Infrastructure:** schema v4, config/identity/project-binding primitives,
   dual-root lifecycle, provider port, role-specific sidecar/recorder contracts,
   and focused migrations/fixtures. Capture remains inactive.
3. **Capture:** durable scanner/read port, admission, enqueue, worker/delivery,
   lifecycle composition, and degradation tests.
4. **Retrieval:** scoped CLI/internal API, owner Settings enablement/binding,
   independent status and Agent Memory search/list UI, i18n, and prompt-inert
   parser contracts.
5. **Scenario closed loop:** catalog, reusable harness, packaged-runtime
   regression, final docs/observations, and issue close-out evidence.

No PR is stacked. Each slice branches from the latest `master` after the prior
slice is approved and merged.

## Scenario Contract

The final catalog is `tests/scenarios/memory_agent_track/` and registers these
stable ids:

| ID | Invariant |
|---|---|
| `MEMORY-AGENT-001` | The absent/default config leaves the second root, scanner, and worker off; every enable starts at the current high water. |
| `MEMORY-AGENT-002` | Every eligible completed interactive or Harness Turn is represented once by its exact dispatch/result pair. |
| `MEMORY-AGENT-003` | Admission excludes every terminal shape that does not satisfy the completed-result invariant. |
| `MEMORY-AGENT-004` | Commit ordering and crash/replay cannot lose or enqueue one source Turn more than once. |
| `MEMORY-AGENT-005` | Agent and project partitions cannot read or write each other's output. |
| `MEMORY-AGENT-006` | Malformed config, legacy Agent identity, and missing project binding fail closed. |
| `MEMORY-AGENT-007` | Agent-sidecar outage leaves chat and Personal Memory healthy. |
| `MEMORY-AGENT-008` | Both role sidecars reject every payload outside their exact owner/shape contract. |
| `MEMORY-AGENT-009` | CLI/UI retrieval is explicit, bounded, inert, and absent from Agent prompts/install paths. |
| `MEMORY-AGENT-010` | Accepted processing with zero cases or skills is a truthful valid outcome. |
| `MEMORY-AGENT-011` | Queue/disk exhaustion skips durably without retaining text or degrading Personal Memory. |

Evidence layers are unit tests for config/identity/admission/store/worker/runtime,
contract tests for provider/sidecar/internal API/CLI/UI, executable catalog
scenarios for the closed loop, and one hermetic packaged EverOS regression. No
test may read or write real `~/.avibe`, restart the local `vibe` service, or use
external provider credentials.
