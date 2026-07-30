# Model Hub L3 Turn Correlation

Status: implementation note for provenance v3 and the orchestrator-delivered,
channel-aware chain/probe v4. Contract files remain read-only to L3 authorship.

## Purpose

Turn provenance is useful only when every recorded attempt belongs to exactly
one Workbench turn. A plausible guess is worse than an explicit absence. This
design therefore uses the existing Workbench turn token as the only turn
identity and fails closed whenever a process-scoped gateway credential cannot
be attributed exactly.

## Runtime Facts

`SessionTurnManager` is the lifecycle authority for Workbench turns:

- `in_flight` owns one `Turn` per Avibe session id;
- the dispatch seam creates `context.platform_specific["turn_token"]`;
- the registered stream sink carries the same token and rejects duplicate
  registration;
- IM and CLI dispatches do not enter this FSM.

The token is therefore already bound to the turn that emits and settles. L3
does not mint another turn id.

Backend process scopes are:

| Backend | Credential scope | Runtime fact |
| --- | --- | --- |
| Claude | Claude composite client identity | One SDK client/process lane per main or agent-specific session identity |
| Codex | `(backend, normalized cwd)` | One app-server transport is reused by every session in that cwd |
| OpenCode | OpenCode server instance | `OpenCodeServerManager` is a singleton shared across all working directories and tracks multiple active sessions |

The OpenCode fact contradicts the requested server-per-session verification.
The shared-server evidence is `modules/agents/opencode/server.py:62-65`
(`OpenCodeServerManager` is a singleton shared across working directories) and
`modules/agents/opencode/caller_context.py:3-6` (the shared `opencode serve`
process multiplexes per-session context through a binding file). The
orchestrator confirmed that frozen v3 already represents this truth through
honest absence, so no targeted v4 is needed: OpenCode provenance is never
written in v2.

## Credential And Turn Registry

`ModelHubTurnGateway` owns one in-memory, process-lifetime registry. A key is
`(backend, process_scope)`, and its bearer token is minted on first use and
remains stable for the lifetime of that process scope. This replaces the
current per-backend token map without changing any backend process model,
gateway wire shape, or runtime fingerprint semantics.

The launch callers thread only the process identity already available at the
owned call sites:

- Claude passes the exact composite cache identity of the main or agent-specific
  SDK client;
- Codex passes its normalized cwd;
- OpenCode passes the singleton server scope.

`resolve_model_hub_launch` asks `SessionTurnManager` for the `Turn` whose
`task` is `asyncio.current_task()`. A match supplies the existing
`turn_token`; no match is an untracked use. This is the key reuse: the
correlator reads the FSM's token binding rather than constructing a parallel
session-to-turn map.

Each registry entry keeps:

- the stable bearer token;
- active tracked turn ids for the scope;
- a sticky ambiguity bit per overlapping tracked turn;
- a sticky `untracked_use` bit for the token lifetime;
- per-turn attempt traces while the turn is active.

The transition rules are:

1. A tracked launch registers its existing turn id under the process scope.
2. An IM/CLI launch has no FSM match and sets `untracked_use`.
3. A gateway request is attributable only when the scope has exactly one
   active tracked turn, no overlap has occurred for that turn, and
   `untracked_use` is false.
4. Two live tracked turns in one scope mark both turns ambiguous. Removing one
   later does not make either historical attempt exact.
5. An untracked use poisons the process-scope token for provenance. This is
   deliberately conservative: the stable credential carries no request field
   that could separate later tracked traffic from the untracked caller.
6. A request that fails the predicate is served normally but contributes no
   attempt to any provenance record.

The registry never serializes the bearer token or exposes it through an API.
When a Claude client cache entry or Codex cwd transport is evicted, its backend
lifecycle hook retires the matching registry scope and bearer token. A later
runtime for the same scope mints a new token. Retirement during a stuck active
turn marks that trace ambiguous before settlement, so eviction cannot turn
partial process traffic into an exact provenance record. In-process
replacement keeps the scope only when the replacement launch has already
registered the current turn against that same Claude session or Codex cwd.

## Attempt Capture And Settlement

The gateway records the classified `RawCallOutcome`, never raw response bodies
or credentials. The service contributes the selected source, resolved model,
channel, mapping flag, and supply state. Streaming attempts are finalized only
after their stream outcome is known.

For Hub launches, the registry also retains the caller-facing model and mapping
decision before the backend CLI replaces the request model with the resolved
target. The gateway accepts that request only when its model matches the
prepared target; a different-model request is untracked use and makes the turn
ineligible for a record. This preserves `requested_model_id` and `via_mapping`
without adding a wire field or a second correlator.

`SessionTurnManager` calls one additive settlement hook before retiring the
`Turn`. The hook receives the existing turn token and the FSM settlement:

| FSM / resolution fact | Provenance outcome |
| --- | --- |
| terminal result after a usable completion | `served` |
| all retryable candidates failed | `exhausted` |
| invalid parameter, protocol/tool failure, or dropped connection | `failed_terminal` |
| no runnable candidate at resolve time | `no_candidate` |
| `settled_by == stopped` | `canceled` |

A dropped connection settles as `no_terminal_result`, not `stopped`, and is
therefore never labeled `canceled`. A user Stop may have no upstream attempt;
in that case `canceled_attempt` is null. When Stop interrupts a known in-flight
attempt, `canceled_attempt` contains only its identity, as required by v3.

The writer emits a record only if all captured attempts passed the exactness
predicate. The timestamp is the FSM settlement time. Records use an atomic,
bounded state file beside the existing Model Hub event store. No placeholder,
coarse record, or attribution-grain field is written.

## Read-Time Absence

`GET /api/models/turns/<turn_id>/provenance` reads an exact record first. If no
record exists, it derives whether the turn is known from the Workbench session
store's persisted output metadata and reads a bounded turn-mode marker captured
from the bound launch at FSM settlement:

- known Direct turn: `provenance_unavailable` /
  `models.provenance.direct_mode`;
- known Hub turn without an exact record:
  `provenance_unavailable` /
  `models.provenance.attribution_ambiguous`;
- unknown id: `turn_not_found`.

The mode marker contains only `turn_id` and the turn-time `direct` / `hub` mode;
it is stored separately from v3 provenance records and never carries an attempt
identity. A known turn without a marker fails closed as
`attribution_ambiguous`. The read path never infers historical mode from mutable
current agent configuration. IM and CLI paths have no FSM turn id and write
neither a marker nor a provenance record.

## Pre-Launch Supply Failure Copy

The resolve boundary in `modules/agents/model_hub.py` is the single mapping
point, so Claude, Codex, and OpenCode inherit the same typed failure copy:

| State | Copy |
| --- | --- |
| self-healing retry | `下一回合已自动换线，直接重试即可` |
| waiting | `模型 {model} 暂时不可用，等待 {source} 于 {retry_at} 自动恢复。` |
| interrupted with blockers | `模型 {model} 当前不可用：{blockers}。请前往 Models 处理。` |
| no enabled source | `模型 {model} 没有已启用的来源。请前往 Models 配置。` |
| no eligible source | `模型 {model} 没有适用于 {backend} 的来源。请前往 Models 配置。` |
| unsupported model | `没有来源支持模型 {model}。请前往 Models 配置。` |

Successful fallback remains silent. Structural causes use
`no_enabled_source`, `no_eligible_source`, or `model_unsupported`; user-facing
text is selected through `vibe/i18n`. Persisted blocker detail keys are mapped
to the same closed ResolutionEvent reason vocabulary and translated before
interpolation; raw i18n keys never appear in launch copy.

## Verification

The acceptance suite contains separate fixtures for:

- two concurrent Web turns sharing one Codex cwd: neither record contains the
  ambiguous attempt;
- the same two turns run sequentially: both records are present;
- one tracked Web turn plus one untracked IM/CLI use in the same scope: the
  tracked record is absent;
- one known OpenCode turn: the shared server writes no record and the read API
  returns `provenance_unavailable` / `models.provenance.attribution_ambiguous`,
  never `turn_not_found`;
- one mapped Hub turn: the record retains the pre-mapping menu model and marks
  every matching attempt `via_mapping: true`; an unexpected request model
  makes the record absent;
- user Stop, dropped connection, and successful control settlements;
- every observed native CLI terminal failure classified before settlement
  (unknown diagnostics use `unclassified_error` without mutating source
  health), so a failed turn cannot be recorded as served;
- Direct, ambiguous-known, and unknown provenance route absences;
- source chain order and model-scoped supply state;
- Hub probe usable-completion, native CLI readiness, and their latency
  partitions;
- single-grain event emission and source referential integrity.

Healthy ambiguity emits no ResolutionEvent.
