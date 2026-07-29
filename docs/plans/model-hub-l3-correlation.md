# Model Hub L3 Turn Correlation

Status: implementation note for frozen contract v3. Contract files remain
read-only.

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
| Claude | Claude composite session identity | One SDK client/process lane per session identity |
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

- Claude passes its composite session identity;
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

## Attempt Capture And Settlement

The gateway records the classified `RawCallOutcome`, never raw response bodies
or credentials. The service contributes the selected source, resolved model,
channel, mapping flag, and supply state. Streaming attempts are finalized only
after their stream outcome is known.

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
record exists, it derives whether the turn is known by querying the Workbench
session store's persisted output metadata for that `turn_id`:

- known Direct turn: `provenance_unavailable` /
  `models.provenance.direct_mode`;
- known Hub turn without an exact record:
  `provenance_unavailable` /
  `models.provenance.attribution_ambiguous`;
- unknown id: `turn_not_found`.

The distinction is live and derived. No absence marker is persisted.

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
text is selected through `vibe/i18n`.

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
- user Stop, dropped connection, and successful control settlements;
- Direct, ambiguous-known, and unknown provenance route absences;
- source chain order and model-scoped supply state;
- probe usable-completion and latency partition;
- single-grain event emission and source referential integrity.

Healthy ambiguity emits no ResolutionEvent.
