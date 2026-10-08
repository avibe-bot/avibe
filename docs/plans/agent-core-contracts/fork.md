# C-10 Fork

Status: **draft**, 2026-10-08. Design only. Points the owner decides are marked **[O-n]** and collected in §18. Every
`file:line` was read on `avibe-agent` at `ce169436c`. After the owner decides, the delta in §16 is applied to the
contracts it touches, and this file is frozen before the Phase 1 lanes fork (README).

A fork is an execution context whose model-visible messages begin with a source context's projection at a **fork
point**, and which then diverges on its own. Fork is the base other features build on: C-9's checkpoints today, and
later an Agent copying itself into a team, managing its own context, and consolidating memory.

## 0. Example

Session P runs on Vibey and has 40 context rows. The user forks from the reply at message 12, whose row has
`context_seq` 23. P keeps working on a later Turn meanwhile.

1. The reservation resolves the fork point. The reply is a response with no tool calls, so the cut is 23. No tool
   call at or before 23 lacks its result, so the cut is legal. It writes Session C with `fork_source_session_id = P`,
   `fork_source_message_id = <message 12>`, and `fork_source_context_seq = 23`, and copies no row.
2. C's first Turn loads P's rows with `context_seq <= 23` and projects them. The result is what P's model saw at 23:
   checkpoints and edits written up to 23 apply, later ones do not. The user's message becomes C's row 24.
3. P commits 41, 42, and so on. P and C are different Sessions with different locks and `context_seq` spaces. C never
   sees what P writes after 23, and P never sees what C writes.
4. P's Watch `w1`, started at row 18, stays P's, and its follow-up goes to P. C's environment block lists only C's
   Watches. C's fork notice says that work started before the fork reports to P.
5. Later C's context passes its threshold. C-9 runs a **side turn** in C: the same idea of a fork point, without a
   new Session. Its prefix is C's latest request, its tools are C's own tool definitions run under the dreaming
   policy, and its reply is folded into one `context_compaction` row in C.

Steps 1–4 make a **fork Session**. Step 5 is a **side turn**. Both start from a fork point.

## 1. One primitive, two carriers

The primitive is the fork point and the prefix it selects (§2, §3). Two carriers use it. They differ in identity,
because identity is what an effect beyond the turn needs.

| | Side turn (ephemeral) | Fork Session (durable) |
| --- | --- | --- |
| Identity | borrows the caller's run: its Session id, route, system prompt, tool definitions, and cancel scope | a new Session with its own Turns, writer lock, `context_seq` space, jobs, Watches, and recovery |
| May do | call the tools its policy allows, provided they finish within the call and need no call instance (§6) | everything its Agent may do |
| Result | a value, which the caller folds into its own context (§9) | nothing, or its replies delivered as inputs to another Session (§9) |
| Persists | one audit row in the caller's Session, plus what the caller's fold commits | its own rows; the prefix by reference |
| Layer | engine, `core/agent_core/agent` | storage and service (`storage/agent_transcript.py`, `core/services/session_fork.py`) |
| Consumers | C-9 checkpoints, memory consolidation, rewriting its own context | the user's "fork from here", `vibe agent run --fork-*`, teammates, delegated exploration |

**Why identity separates them.** A command is a job keyed by its call instance: the committed response that made the
call, in a Session (`core/agent_core/tools/base.py:28-42`). Watches, T2 recovery, approvals, and the user's access
all hang off a Session. A side turn commits no response, so its calls have no instance (`agent/loop.py:1667`), and
`bash` refuses to start (`tools/bash.py:213-215`). So anything that may act beyond its own turn needs a Session, and
anything that only thinks does not. Borrowing the caller's identity is also what keeps a side turn's prefix
cache-warm (§3).

The alternative, a durable fork as an ephemeral fork plus a persistence flag, is rejected. It would either give side
turns jobs that no Session owns, or give every checkpoint its own Session row.

## 2. Fork point

```text
ForkPoint = (as_of, units)
  as_of  a committed context_seq of the source's context (its own rows or its inherited prefix), or 0: the empty
         prefix, before the first row
  units  None, or a number of units (C-9 §5) of the view at as_of
prefix(source, point) = context_view(rows(source), fork_point=as_of).prefix(units, or all units)
```

Both parts exist today:

- `context_view(entries, fork_point=...)` keeps the rows with `context_seq <= fork_point`
  (`harness/projection.py:105-108`, `:279`).
- `ContextView.prefix(cut)` keeps whole units (`:89-99`). A unit is an input, or a response together with its tool
  results (`:50-69`).

**Settled.** A fork point is legal only when it is settled: every tool call in the rows up to `as_of` has its result
in those rows, so `open_tool_calls` over them is empty (`projection.py:273`). Unit boundaries are always settled. As
a result, a fork never inherits an open call: it inherits settled calls with their results as history, and never
settles or looks up a call it did not make (F3). A settled point may fall mid-Turn: after a complete tool batch, or
between a response with no tool calls and a steer.

- **A mid tool batch point is never legal.** Projection would have to invent interrupted results, and two Sessions
  would then settle one call.
- **A job handed to a Watch does not block a point.** Its call already has a committed result ("still running, now
  Watch `<id>`"), so the point is settled. The job keeps running for the Session that started it (F4).
- **A foreground job still running blocks a point.** Its call is open.

**How each carrier uses the coordinates.**

- **A fork Session uses `units = None`.** The child stores one number, `fork_source_context_seq = as_of` (C-5 §4).
  Its projection of the inherited rows is the source's view at `as_of`.
- **A side turn uses `as_of` = the caller's latest committed row.** That row is the run's own view. `units` selects
  a prefix of that view, which C-9's rolling checkpoint needs (`view.prefix(cut)`, `loop.py:1280-1282`). A side turn
  never forks an older view in v1: a side turn exists to reuse a byte prefix of the request just sent (§3).

**Why two coordinates.** A later checkpoint or edit changes how earlier rows project.

- The view at `as_of` is what the model saw then. A fork "from message 12" wants this view.
- A prefix of the current view is what the model sees now, cut short. C-9's rolling checkpoint wants this one.

The view at `as_of` also defines the prefix of a cut in a compacted region:

- If a checkpoint was written at or before `as_of`, the prefix is that checkpoint plus its kept tail.
- If a checkpoint was written after `as_of`, it did not exist then. The prefix is the rows as they were (A4).

### Resolving a fork Session's point

This replaces `resolve_fork_anchor_seq` and `context_bound` (`storage/agent_transcript.py:492-536`). Those match
`agent_events` rows to an anchor message by wall clock (`:528-534`); this rule uses only `context_seq` and row
shape.

| Request | `as_of` |
| --- | --- |
| a response with no tool calls: a reply (`result`, `error`, a hidden final `assistant`) or an answer before a steer | its `context_seq` |
| a response with tool calls | the `context_seq` of its last tool result, which ends its unit; refused while a result is missing (`session_fork_point_unsettled`) |
| a consumed input (`user`, `harness`, `agent_initiated`, `annotation`) | the largest settled `context_seq` before it in the source's context, or 0 when there is none, so the fork asks again from there |
| a row without `context_seq`: display-only, queued, or removed | refused (`session_fork_point_not_in_context`) |
| no point, and the source has a live Turn | the largest settled `context_seq` before that Turn's first consumed input (of the whole context while the Turn has consumed none), so the live Turn is trimmed |
| no point, and no live Turn | the largest settled `context_seq` of the source's context |

- **0 is the empty prefix.** A child with `fork_source_context_seq = 0` inherits no row, and its first own row is
  `context_seq` 1. `fork_link` already accepts 0 (`agent_transcript.py:677-678`), and `context_view` over no row is
  empty.
- **The requested row must be one of the source's own rows.** A row the source inherited is refused
  (`session_fork_point_inherited`), and the error names the Session that owns it. Forking that Session at that row
  gives the same prefix, and the lineage then names the Session the message belongs to, so a link to the message
  resolves in that Session.
- Rows that receive a `context_seq` later never move into or out of the prefix (C-5 §4).

**Trimming the live Turn changes today's behavior.**

- Today a fork of a running Vibey Turn takes the running Turn's first input as its anchor (`session_fork.py:263`,
  `:389-399`). It includes that input row (`agent_transcript.py:521-527`), so the child starts with a request that
  is unanswered and not its own.
- Trimming removes that. It aligns Vibey with Codex and OpenCode, which already trim the live Turn
  (`session_fork.py:27`, `:269-286`).
- Claude Code's native fork copies whatever is on disk when the child first runs. A test pins this
  (`tests/test_ui_session_stream.py:1101-1134`).

## 3. Prefix identity

**F1 (identity).** A fork's messages up to its cut are `prefix(source, point)`, computed by the one projection.

- A fork Session recomputes it on every request.
- The context content of a committed row never changes (C-5 §5), so every request gets the same bytes, whatever
  either Session commits later.

**Cache warmth** is a separate property. A provider reuses a cached prefix only when the endpoint, the tool
definitions, and the system prompt also match. For Anthropic, tool choice and reasoning settings must match too,
because they are rendered into the prompt (C-9 §6).

**F2 (warm side turns).**

- A side turn sends the caller's endpoint, system prompt, tool definitions, tool choice, and reasoning settings
  unchanged.
- Its messages begin with the caller's latest request's messages through the cut, byte for byte.
- It narrows capability when a call executes, never in the tool list. Removing a denied tool from the request would
  shift every byte after it.

**A fork Session is cache-cold today.**

- Vibey's system prompt carries the Session's own id, plus a fork note (`core/system_prompt_injection.py:228-231`,
  `core/prompts/forked-session.md`). So a child's first request shares only the tool definitions with its source.
- For a user's fork, correct identity is worth one cold prefix.
- For N teammates forked from one large context, it is N cold copies of that context.
- The remedy is a Session-invariant system prompt, with the Session id moved into the environment block of the
  first input (C-7 §8). That is reserved for Phase 2 (§17).

## 4. Storage: by reference

**Decision:** a fork Session references the source's rows `<= as_of` and copies nothing. This is C-5 §4, already
implemented:

- `fork_link` reads `fork_source_session_id` and `fork_source_context_seq` (`agent_transcript.py:662-679`).
- `_ancestry` walks the chain, narrowing the bound at each step (`:682-698`).
- `_load` reads the whole chain in one snapshot (`:375-388`).
- A child's first `context_seq` follows its anchor (`_next_context_seq`, `:419-424`).

Consequences:

- **`context_seq` space.** It is unique per Session (`storage/models.py:508-512`, `:692-696`). A child continues at
  `as_of + 1`, and the source keeps its own numbers. The merged order inside each child is total.
- **Shared identity.** Inherited rows keep their ids.
  - A `context_edit` in the child may target an inherited result by id.
  - The child's C-9 anchor may be an inherited response, when the route is the same (C-9 §2).
- **Call instances.** An inherited call keeps its instance, which names the response of the Session that made it.
  Because cuts are settled, the child never acts on one. T2's branch for inherited open calls
  (`modules/agents/vibey/agent.py:726-731`) is deleted in Phase 1. `call_instance_result` stays, for J5 (`:412-422`).
- **Deletion and retention (F8).**
  - No path deletes context rows today. A Session with messages is archived, not deleted
    (`storage/sessions_service.py:2643-2689`). Every `agent_events` deletion selects `visibility = 'trace'` only: trace
    retention (C-5 §5), the skill-trace cleanup (`storage/skill_observability.py:19`, `:398`), and an empty
    Session's delete (`storage/sessions_service.py:2694-2698`).
  - Any future deletion or retention must keep every row that a live descendant's prefix includes. It needs the
    descendant lookup (§5) to do so.
- **Media.** Vibey context images are content-addressed files, loaded by token without a Session check
  (`modules/agents/vibey/media.py:47-58`, `:82-106`). So a child replays the images it inherits, and nothing deletes
  media today. The product-wide retention follow-up (plan §10) must count a fork's inherited rows as references (F8).
- **Scratch is not forked.**
  - The scratch directory belongs to its Session (`agent.py:544`) and is not snapshotted. If a child reads a scratch
    path it finds in an inherited checkpoint, it reads the source's current file.
  - This is a known limit. Scratch is a checkpoint turn's working area, and the checkpoint carries its conclusions in
    its own text (C-9 §6).

**Rejected: copying the rows** for independent deletion.

- It breaks shared identity: copied results get new ids, so inherited edits, anchors, and call instances would all
  need rewriting.
- It multiplies storage for every teammate of a long context.
- It pays for a kind of deletion that no path performs.

## 5. Lineage

- **Child to source.** The reservation writes top-level metadata on the child Session (`session_fork.py:368-399`):
  - `fork_source_session_id`
  - `fork_source_message_id`: the requested message, or for "latest" the anchor where the backend trims
  - `fork_source_context_seq`: Vibey only
  - `fork_created_at`

  Phase 1 adds no new shape.
- **Source to children.** This is absent today. Lineage is recorded only child to parent, and the UI only shows the
  child's banner (`ui/src/components/workbench/ChatPage.tsx:4071-4083`). Retention, teams, and runaway bounds need
  the reverse lookup. Phase 2 adds:
  - an expression index on `json_extract(metadata_json, '$.fork_source_session_id')`;
  - one metadata key, `fork_initiator`: `user`, or the requesting Session and run.

  No column is added. The metadata keys stay the one record.
- **Side turns.** Each leaves one `fork_turn` audit row in the caller's Session, recording its point, purpose,
  policy, and outcome (§7). That row is a side turn's lineage.

## 6. Capability

**F6:** a fork never has a capability its source lacks.

**Side turn.** Its policy names the tools it may run, each with a rule:

- `allow`: the tool runs.
- `scratch`: `write` or `edit` runs only into the caller's flat scratch root, through the root's descriptor (C-9 §6).

Any tool the policy does not name is denied with the policy's text and never runs. Constraints on policies:

- An allowed tool must finish within the call and need no call instance. That rules out `bash`, which starts jobs,
  and anything that asks the user, since a side turn has nowhere to ask.
- Policies are code. The contract that consumes a policy owns it, and this file lists it.
- v1 has one policy, `dreaming` (C-9 §6, `agent/checkpoint.py:29-35`): `read` is allowed, and `write` and `edit` go
  to scratch.
- A tool missing from the caller's request cannot be granted, because the request is the caller's.

**Fork Session.** Its tools are its Agent's. The reservation already checks that the caller has:

- editor access to the source (`session_fork.py:200-231`);
- chat access to the destination Project (`:232-246`);
- selection authority for the Agent the fork runs as (`:155-174`, `:288-327`).

It also requires the source's backend (`:295-298`). A fork requested by an agent runs with the authority of the
requesting Turn's caller context, which the Harness records (`vibe/cli.py:6898`), and never more.

**Approvals.** Vibey has no approval flow yet: its Agent runs with `hooks=()` (`agent.py:536`), and the flow is
planned for P3 (plan §7). The order is fixed now:

- The policy is checked first, then approval.
- A side turn denies any call that would ask.
- A fork Session asks in its own Session, never in its source.

**Vibey is always on.** A fork of a Vibey Session runs on the `vibey` backend, which is always enabled. The
reservation's backend and Agent checks are unchanged.

## 7. Side turn

C-9's checkpoint turn becomes the generic side turn: `loop.py:1461-1618`, its tool step `_checkpoint_tool`
(`:1645-1670`), and `CheckpointPolicy` (`agent/checkpoint.py`). C-9 keeps what makes it a checkpoint (§12). A side
turn runs inside the caller's run, in these steps:

1. **Request.**
   - It is composed by the caller's request builder (`_built`, `loop.py:622-649`) from
     `prefix(caller, (latest, units))`, then `prompt`, then the turn so far.
   - It uses the caller's endpoint, system prompt, tool definitions, and reasoning settings. `max_tokens` comes from
     the spec, per route.
   - The request actually composed is budgeted. One that cannot fit is never sent, and the turn fails as an overflow.
   - The dry run that decides whether to start (C-9's `_fork_fits`, `:1304-1309`) composes the same request (C-9 §10,
     invariant 1).
2. **Model.** It goes through the caller's model call (`_model`, `loop.py:651`), so it shares that call's:
   - retries;
   - attempt ledger, under purpose `side_turn`;
   - admission of every response against the committed rows plus the turn's own messages (C-9 §10, invariants 4
     and 6).

   Its emit is silent.
3. **Tools.** Each call goes through one pipeline, in this order:
   1. **The budget.** Rounds must be at most `max_rounds`, and `room` at least `room_floor`. `room` is derived from
      the latest request's budget, as C-9 §6 states.
   2. **The policy.**
   3. **Execution.** A scratch write goes through the root's descriptor, as joined work that an abort waits for.
   4. **The bound.** The result is cut to `room - room_slack`.

   The policy's fixed texts are never cut.
4. **End.** A response that stops with `stop` and calls no tool is the reply. Anything else fails the turn: an
   overflow, an error, a response refused at admission, a length stop, or calls after the budget closed.
5. **Fold.** The caller's `fold(reply)` decides what enters the caller's context, if anything, and commits it. For
   C-9 that means validating the reply, building the `Compaction` row, and committing it. If the fold fails, the turn
   fails.
6. **Audit (F7).** The side turn writes one `fork_turn` audit row (`visibility = 'audit'`, no `context_seq`) on every
   exit except an abort. The row holds:
   - its purpose, policy, and point;
   - every attempt's message, from the ledger, and the turn's tool results;
   - its usage, rounds, outcome, and error;
   - the id of the folded row.

Rules that hold for every side turn:

- Nothing else it produces is context, and nothing is shown.
- An abort of the caller's run aborts it, and the cancelled scope writes nothing more.
- At most one side turn is in flight per Session, because it runs inside the caller's run, the Session's only run
  (C-9 §10).

**The audit row replaces C-9's `CheckpointTurn`** (`context_checkpoint_turn`, `storage/agent_transcript.py:94-95`).
C-9's own fields, `reason` and `mode`, move into `detail`. `avibe-agent` has not shipped, so no reader of the old kind
needs to remain (#2386).

**Idle side turns are reserved.** In v1 a side turn runs only inside a run. Memory consolidation will need one while
the Session is idle. The adapter must then hold the Session's writer lock (`agent.py:1184-1212`), so the view cannot
move; the API reserves this (§15).

## 8. Fork Session

- **Reservation.** `reserve_forked_session` (`session_fork.py:177`) gains `at_message_id`.
  - For `vibey`, it resolves `as_of` (§2) inside the reservation's transaction and writes the metadata (§5).
  - For a backend without point-in-time support, `at_message_id` is refused with `session_fork_point_unsupported`
    (§11).
- **It owns nothing its ancestors started (F4).** A side turn, by contrast, starts nothing.
  - Jobs, Watches, Tasks, delegated runs, and scratch stay with the Session that started them. In a nested fork (P,
    then C forked from P, then D forked from C), a job P started still reports to P, not to D's source C.
  - Watches and Tasks are run definitions of their own Session, and the reservation copies none of them. A job's
    Watch follows up to the job's Session (`core/watches.py:757-808`).
  - The child's environment block lists only the child's Watches (`agent.py:1041`).
  - The child's first input carries a fork notice, which the adapter renders from the fork's facts. It says the
    Session is a fork of `<title>`, and that every command, Watch, Task, and run in the inherited history stays with
    the Session that started it and reports there, not here. Today's fork prompt (`core/prompts/forked-session.md`)
    covers only the Session id.
- **Recovery.**
  - The child's recovery is its own: T1–T4 and J1–J6 apply to its own rows and jobs.
  - A settled cut leaves it nothing of the source's to settle.
- **Turns.**
  - The child's Turns are its own. Each Session has at most one live Turn (`uq_session_turns_live_session`,
    `storage/models.py:875-879`).
  - The child has its own writer lock and its own transcript lock (`agent.py:1184-1212`, `agent_transcript.py:331-337`).
- **Display.**
  - The child's Workbench shows its own rows and the fork banner (`ChatPage.tsx:4071-4083`). For a fork taken at a
    message, the banner is extended to name that message, with a link to it in the source Session, which owns it
    (§2) **[O-3]**. A fork at the latest point keeps today's banner, which links to the source Session.
  - The inherited prefix is not rendered in the child.
  - The earlier-record hint already names the source and the seq (`modules/agents/vibey/context.py:86-87`).

## 9. Merge-back

Results flow back in one of three ways. Transcripts are never merged.

1. **Return value (side turn).** The caller's `fold` turns the reply into one of its own rows (for C-9, a
   `context_compaction` row), or into nothing.
2. **Message (a fork Session started through the Harness).** The child's final reply reaches the requesting Session
   as a Harness callback input. That Session's own loop consumes it under its own lock: as a steer while a run is
   active, or as a new Turn when idle. This exists today for `vibe agent run`.
3. **Nothing (user forks).** The user continues in the child.

**Rejected:** writing a child's rows into the source, or a merge that replays a child's turns there. Both create two
writers of one context and a second copy of rows (plan §3, hard constraint 4).

## 10. Concurrency and the four harms

A side turn runs inside its caller's run. A fork Session is its own Session and often runs at the same time as its
source, as teammates would. The four harms are the decision rule for both (the review-loop rule of 2026-10-02, an
orchestrator decision the owner may revise): data loss, a runaway or hung process, a security issue, and false
information shown to the model.

| Harm | Risk | Rule |
| --- | --- | --- |
| Data loss | two writers on one context; a source deleting rows a child reads | one writer per Session, with its own `context_seq` space and locks; a fork Session never writes a source row (F5); rows `<= as_of` never change (C-5 §5), so no lock spans Sessions; nothing deletes a referenced row (F8); merge-back only appends, through the receiver's own loop (§9) |
| Runaway | an Agent forking Agents that fork in turn; a side turn that keeps calling tools | a side turn is bounded by its rounds and room, at most one runs per Session, and C-9's per-run bound applies; fork Sessions: see below |
| Security | a fork gaining a capability | F6 (§6) |
| False information | a child believing it owns its source's Watches or jobs; a side turn's messages leaking into context | the fork notice and the child-only environment block (§8); a side turn's messages are audit only (§7) |

**Runaway bounds for fork Sessions.**

- Today only the Harness's process-wide limit of 8 concurrent runs bounds them (`core/scheduled_tasks.py:3971`).
- Model Hub does not cap spend, by design (`core/handlers/model_hub/usage.py:18-24`).
- Delegation depth is recorded (`parent_run_id`, `vibe/cli.py:6898`) but not bounded.
- Plain delegation, `vibe agent run --agent`, recurses the same way, so the bound on depth and on live descendants
  belongs to the Harness, not to fork **[O-4]**.

**Model Hub.**

- A side turn uses the route of the request it forks, and resolves again on retry, as its caller does.
- A fork Session resolves its own route for its own model. Model Hub sees the two Sessions separately
  (`process_scope = vibey:<session_id>`, `agent.py:516-517`).
- Usage is recorded per Session, in response rows and in the attempt and `fork_turn` audit rows. A later per-lineage
  budget would sum those.

**Turn ownership.**

- A side turn has no Turn and emits no event of its own. It belongs to its caller's run (C-4).
- A fork Session's Turns go through the shared Turn owner like any Session's.

## 11. Layers and surfaces

| Layer | Owns | Phase 1 change |
| --- | --- | --- |
| `core/agent_core/harness` | `ForkPoint`, `settled`, `fork_cut`, `fork_prefix` (pure, §2) | add them; `fork_prefix` is `context_view` plus `prefix` |
| `core/agent_core/agent` | side turn: `SideTurn`, `ForkPolicy`, `DREAMING`, `Agent.side_turn`, `Agent.side_turn_fits` | extract them from C-9 (§12) |
| `storage/agent_transcript.py` | storage by reference, ancestry, `resolve_fork_point` (SQL around `fork_cut`) | replace `resolve_fork_anchor_seq` and `context_bound` |
| `modules/agents/vibey` | the fork notice on the first input; T2 without the inherited branch | as listed |
| `core/services/session_fork.py` | the reservation for every backend, `at_message_id`, error codes | add `at_message_id` |
| `modules/agents/catalog.py` | `FORK_AT_MESSAGE_BACKENDS`, a capability-specific set (C-8) | add `{"vibey"}` |
| surfaces | CLI, HTTP, Web | below |

### Backend support

| Backend | Fork today | Point-in-time | Phase |
| --- | --- | --- | --- |
| `vibey` | by reference, at the latest point (`session_fork.py:381-399`) | yes (§2) | 1 |
| `codex` | `thread/fork` (`modules/agents/codex/agent.py:3316-3421`); `lastTurnId` is used only to trim a live Turn (`:3393-3408`) | `lastTurnId` accepts any completed Turn, and the map from Avibe message to Codex Turn already exists (`session_turns.native_turn_id`, `core/session_turns.py:8008`) | 3, first |
| `claude` | `resume` with `fork_session` (`core/handlers/session_handler.py:1558-1563`, `:1730-1731`) | the Agent SDK's `resume_session_at` takes a message UUID, used with `fork_session`; Avibe stores no map from message to UUID | 3; needs the map |
| `opencode` | `POST /session/{id}/fork`; `messageID` is sent only to trim (`modules/agents/opencode/server.py:818-837`) | `messageID` accepts any message, and the fork excludes it; Avibe stores no map from message to OpenCode id | 3; needs the map |

### Surfaces in v1 [O-2]

- **CLI.** `vibe agent run --fork-session <id> --at <message-id>`, and `--fork-self --at <message-id>`. `--at`
  requires one of the fork flags. The CLI examples in the injected system prompt are live callers, so they need
  parser-backed coverage (AGENTS.md §7).
- **HTTP.** `POST /api/sessions/<id>/fork` accepts `{"at_message_id": ...}`. Today the route reads no body
  (`vibe/ui_server.py:9377-9400`), and the UI posts `{}` (`ui/src/context/ApiContext.tsx:3702-3703`).
- **Web.** A "Fork from here" action on a Vibey reply. It is a new UI element, so an approved `design.pen` frame
  comes first **[O-3]**.
- **IM.** None. The owner decided against slash commands.
- **A fork tool for Agents.** Not in v1 **[O-4]**. Agents fork through `vibe agent run --fork-self [--at]`, and the
  result returns by callback. A tool with typed merge-back, and side turns for Agents, come with teams.

## 12. C-9 on the primitive

| C-9 today | After |
| --- | --- |
| `_fork`, `_fork_base`, `_fork_fits`, `_ForkTooLarge` (`loop.py:1280-1309`, `:207`) | the side turn's request and dry run (§7 step 1) |
| the checkpoint turn's loop (`loop.py:1461-1618`) | `Agent.side_turn`; `_checkpoint` keeps the events, the fold, and the unproductive count |
| `_checkpoint_tool` (`:1645-1670`); C-9 §10 invariant 2 | the side turn's tool pipeline (§7 step 3) |
| `CheckpointPolicy`, its table, `DENIED`, `BUDGET_USED` (`agent/checkpoint.py`) | `ForkPolicy` and the value `DREAMING` |
| `CHECKPOINT_TOOL_ROUNDS`, `_FLOOR`, `_SLACK` (`harness/context.py:81-85`) | fields of `DREAMING` |
| the `CheckpointTurn` audit (`context_checkpoint_turn`) | the `ForkTurn` audit (`fork_turn`) |
| ledger purpose `checkpoint` (`loop.py:193`) | `side_turn` |

**What C-9 keeps.**

- When to compact: the trigger, the overflow ladder, and the bounds.
- Which prefix to use: the whole view for a normal checkpoint, or `units` from `rolling_cut` for a rolling one.
- The checkpoint request and `max_tokens = min(16,000, O)`.
- The validator, the row, and the events.

Behavior does not change. The C-9 tests pass, with only the audit kind renamed.

**Concept count.** Before, 8:

1. Session fork at the latest point, with per-backend trimming.
2. C-5's fork by reference.
3. Resolving an anchor by message and clock.
4. T2's settling of inherited open calls.
5. C-3 §7's "snapshot and fork".
6. C-9's forked request.
7. C-9's tool pipeline and policy.
8. C-9's audit row.

After, 3:

1. The fork point: one cut rule and one prefix function.
2. The side turn.
3. The fork Session.

**Deleted:**

- the clock match and `context_bound`;
- T2's inherited branch;
- C-3 §7's fork text. `snapshot()` stays, as the state of the `after_run` outcome (`loop.py:323-326`, `:575`), not as
  a fork API;
- C-9 §6's tool bullets and §10 invariant 2, which move here.

## 13. Future consumers

None of the three below needs a different fork primitive. Each needs something the fork point and its two carriers
already provide, plus work outside fork.

- **Teammates: an Agent copies itself.**
  - How it uses fork: the Agent reserves N fork Sessions at a settled point of its own context, with `--fork-self`
    now or a tool later. They run in the background with one task message each. Each shares the prefix by reference
    and runs concurrently as its own Session, and their replies come back as Harness inputs (§9).
  - Needs from fork: nothing more.
  - Needs elsewhere, reserved for Phase 2: the descendant lookup and `fork_initiator` (§5); the Harness bound on
    depth and live descendants **[O-4]**; a Session-invariant system prompt, so that N forks of one large context hit
    the cache (§3).
- **Self context management.**
  - Explore and come back: a background fork Session with full tools, because exploring needs `bash`. Its reply
    returns as a message.
  - Go back: `--fork-self --at <message>` continues from an earlier point. Phase 1 provides it.
  - Rewrite part of its own context: a side turn with `dreaming` over a prefix, folded into a row. Summarizing a
    middle range while keeping the head needs a range checkpoint row in C-5 and C-9 projection, not a change to fork.
- **Memory.**
  - How it uses fork: a side turn with a `memory` policy, under which memory tools are allowed and the rest denied
    (C-9 already reserves that row, `checkpoint.py:34`). It sends a consolidation prompt and uses a fold that commits
    nothing: its effect is the memory tools' writes.
  - Those writes must be idempotent upserts, because a crash loses the turn.
  - Needs: the memory tools, and side turns while the Session is idle (§7).

## 14. Invariants

| ID | Invariant | Owner | Proof |
| --- | --- | --- | --- |
| F1 | A fork's messages through its cut equal `prefix(source, point)`, and a fork Session gets the same bytes on every request, whatever either Session commits later | harness, storage | a source with checkpoints and edits both before and after the cut, plus a cut before a later checkpoint; after N commits in each Session, the child's projection through `as_of` serializes equal to `project(source, fork_point=as_of)` |
| F2 | A side turn's first request has the caller's endpoint, system prompt, tool definitions, tool choice, and reasoning settings, and its messages begin with the caller's latest request through the cut | agent | the stub's request log, for normal and rolling side turns |
| F3 | Every fork point is settled; a fork never inherits an open call, and never settles or looks up a call it did not make (settled calls are inherited as history, F1) | harness, storage | every row kind in §2's table, including mid batch, a job handed over, a running foreground job, a Session's first input (0), and an inherited row (refused, naming its owner): resolved settled, or refused |
| F4 | A fork Session owns nothing an ancestor started: jobs, Watches, Tasks, runs, scratch stay with the Session that started them. A side turn starts nothing: its calls have no call instance | vibey, service, agent | a child of a source with a live job Watch neither lists it nor receives its follow-up, and its first input carries the notice; a side turn's `bash` call starts no job even when a policy allowed it |
| F5 | A fork Session never writes a row of its source. A side turn writes only its audit row and, after the reply, what its fold commits | agent, storage | the source's rows before and after a child's Turns; the caller's rows after a failed and after a successful side turn |
| F6 | A fork has no capability its source lacks | agent, service | a side turn's call to every tool its policy does not allow is denied and never runs; a reservation without editor, chat, or selection authority is refused |
| F7 | One `fork_turn` audit per side turn, on every exit except an abort, with its purpose, point, policy, messages, usage, outcome, and folded row | agent | each exit path of the turn, as C-9 §10 invariant 4 tests it today |
| F8 | No context row is deleted while a live descendant's prefix includes it | storage | holds today because no path deletes a context row (§4); Phase 1a adds a contract test that every deletion path keeps `visibility = 'context'` rows and the `messages` rows of a Session with context; any later retention adds its own proof |

## 15. API sketch

These are types and signatures, not code. Names follow the surrounding modules.

```python
# core/agent_core/harness/fork.py (pure)
@dataclass(frozen=True)
class ForkPoint:
    as_of: int                    # a committed context_seq of the source context, or 0: the empty prefix
    units: Optional[int] = None   # side turns: the first `units` units of the view at as_of

class ForkPointError(ValueError):
    code: Literal["session_fork_point_not_in_context", "session_fork_point_inherited",
                  "session_fork_point_unsettled"]

def settled(entries: Sequence[ContextEntry], as_of: int) -> bool: ...
def fork_cut(entries: Sequence[ContextEntry], target_row_id: Optional[str], *,
             live_input_seq: Optional[int]) -> int: ...                    # §2's table
def fork_prefix(entries: Sequence[ContextEntry], point: ForkPoint) -> tuple[Message, ...]: ...

# core/agent_core/agent/fork.py (replaces agent/checkpoint.py)
Rule = Literal["allow", "scratch"]

@dataclass(frozen=True)
class ForkPolicy:
    name: str                     # recorded in the audit
    tools: Mapping[str, Rule]     # a tool not named here is denied and never runs
    denied: str                   # the result text of a denied call
    budget_used: str              # the result text once the budget is closed
    max_rounds: int
    room_floor: int
    room_slack: int

DREAMING: ForkPolicy              # C-9 section 6: read allowed; write and edit to scratch; 5 rounds; 4,000 / 1,000

@dataclass(frozen=True)
class SideReply:
    message: AssistantMessage     # stop_reason "stop", no tool calls
    rounds: int
    usage: Optional[Usage]

@dataclass(frozen=True)
class Folded:
    row: Optional[ContextEntry]   # what the fold committed, if anything
    error: Optional[str] = None   # a fold that failed fails the turn

@dataclass(frozen=True)
class SideTurn:
    purpose: Literal["checkpoint"]                         # grows with its consumers ("memory", ...)
    units: Optional[int]                                   # the caller's current view: all of it, or its first units
    prompt: UserMessage
    policy: ForkPolicy
    max_tokens: Callable[[ModelCapabilities], int]         # per route; C-9: min(16,000, O)
    fold: Callable[[SideReply], Awaitable[Folded]]
    detail: Mapping[str, Any] = field(default_factory=dict)  # purpose fields for the audit; C-9: reason, mode

@dataclass(frozen=True)
class SideTurnResult:
    outcome: Literal["completed", "failed"]
    folded: Optional[ContextEntry]
    error: Optional[str]
    overflow: bool                # its request could not fit, or the provider refused it as overflow

class Agent:
    def side_turn_fits(self, turn: SideTurn, route: ModelSelection) -> bool: ...
    async def side_turn(self, turn: SideTurn, route: ModelSelection) -> SideTurnResult: ...
    # v1: only while a run is active. Reserved: while idle, under the adapter's Session writer lock.

# storage/agent_transcript.py
def resolve_fork_point(conn: Connection, source_session_id: str, at_message_id: Optional[str]) -> int: ...

# core/services/session_fork.py
def reserve_forked_session(*, source_session_id: str, at_message_id: Optional[str] = None,
                           ...) -> SessionForkResult: ...
# SessionForkError codes added: session_fork_point_unsupported, session_fork_point_not_in_context,
# session_fork_point_inherited (details name the owning Session), session_fork_point_unsettled

# modules/agents/catalog.py
FORK_AT_MESSAGE_BACKENDS: frozenset[str]   # {"vibey"} in Phase 1
```

```text
vibe agent run (--fork-session <session-id> | --fork-self) [--at <message-id>] --message ...
POST /api/sessions/<session-id>/fork   {"at_message_id"?: string}
```

The `ForkTurn` audit (`agent_events.event_type = 'fork_turn'`, `visibility = 'audit'`, no `context_seq`) replaces
`CheckpointTurn`. Its required fields:

| Field | Value |
| --- | --- |
| `version` | `1` |
| `purpose` | `checkpoint` |
| `policy` | `dreaming` |
| `point` | `{as_of, units}` |
| `outcome` | `completed` or `failed` |
| `messages` | as in `CheckpointTurn` |

Its optional fields are `detail` (for `checkpoint`: `{reason, mode}`), `error`, `folded_event_id`, `rounds`, and
`usage`.

## 16. Contract delta at freeze

| File | Change |
| --- | --- |
| `transcript.md` §4 | `anchor_seq` resolution becomes §2's fork point rule; add a pointer to C-10 |
| `transcript.md` §5 | point to F8 |
| `transcript-rows.schema.json` | `CheckpointTurn` becomes `ForkTurn` (§15) |
| `context.md` §6 | the tool bullets point to C-10 §7 |
| `context.md` §10 | invariant 2 points to C-10 §7; the audit kind is renamed |
| `context.md` §12 | the reserved memory row points to C-10 §13 |
| `loop-control.md` §7 | "Snapshot and fork" points to C-10 |
| `backend-registration.md` | lists `FORK_AT_MESSAGE_BACKENDS` as a capability-specific set |
| `README.md` and plan §5 | the C-10 row; this PR adds it as a draft |

## 17. Phases

| Phase | Scope | Acceptance (properties) |
| --- | --- | --- |
| 0 | this contract; owner decisions [O-1]–[O-4]; the §16 delta, then freeze | owner approval; `pr-delivery-loop` gates |
| 1a | point-in-time fork for Vibey: `settled`, `fork_cut`, `resolve_fork_point` (no clock); trimming the live Turn; `at_message_id` with error codes; `FORK_AT_MESSAGE_BACKENDS`; CLI `--at`; the HTTP body; the fork notice; T2's inherited branch removed; the Web action and banner once the frame is approved | F1, F3, F4, F6 (reservation), F8 pin; an E2E on Workbench: fork from an earlier reply while the source runs, continue in the fork, and the source is unaffected (the E2E wave's S8) |
| 1b | C-9 on the side turn, as a pure refactor: `ForkPolicy` and `DREAMING`, `Agent.side_turn`, the `fork_turn` audit | F2, F5, F6 (policy), F7; the C-9 suite passes with only the audit kind renamed |
| 2 | teammates and Agents: the descendant lookup and `fork_initiator`; the Harness bound [O-4]; a Session-invariant system prompt for warm fork Sessions; an Agent fork tool; side turns while idle | decided when the teams design is written |
| 3 | point-in-time for native backends: Codex through `lastTurnId` from `session_turns.native_turn_id`, then Claude (store message UUIDs; `resume_session_at`) and OpenCode (store message ids; `messageID`) | F1 per backend, to the extent its native store allows |

1b follows 1a; they are not parallel lanes. Both edit `storage/agent_transcript.py` (1a replaces the fork anchor
resolution; 1b renames the audit kind), and 1b records the `ForkPoint` that 1a adds in `harness/fork.py`. 1b
reopens review on C-9's code, the most-reviewed code on `avibe-agent`. Keeping 1b a pure refactor, with C-9's tests
unchanged, keeps that review to the move itself.

## 18. Owner decisions

| ID | Decision | Recommendation |
| --- | --- | --- |
| O-1 | The model: one primitive (the fork point) with two carriers separated by identity. A side turn can do nothing that outlives its turn; a fork Session does everything else. C-9's checkpoint becomes the first side turn. | Yes |
| O-2 | Phase 1 scope: point-in-time fork for Vibey only, on CLI, HTTP, and Web. Native backends come in Phase 3, Codex first; Claude and OpenCode each first need a message-id map. Until then, those backends keep forking at the latest point. | Yes |
| O-3 | Web UX: "Fork from here" only on Vibey replies. The fork shows the banner naming that message, not the inherited history. Not in v1: "edit and resend" on a user message (the API supports it as "fork before this input"), and rendering inherited history in the child. The action needs a `design.pen` frame. | Yes, replies only, with the banner |
| O-4 | Forks requested by Agents. There is no fork tool in v1. Bounding self-replication: an Agent can already recurse with `vibe agent run --fork-self` or `--agent`, bounded only by the 8-run concurrency limit. | A Harness bound on depth and live descendants per root, landing in Phase 2 before the tool. Pull it into Phase 1 only if today's exposure should close now: Phase 1 adds no new exposure, because user forks are not recursive |

Decided in this lane, all reversible:

- A fork Session at the latest point trims a live Turn for Vibey (§2).
- The audit kind is renamed `fork_turn` (§7).
- Scratch is not forked (§4).
- Making the system prompt Session-invariant waits for Phase 2 (§3).

## 19. Rejected alternatives

| Alternative | Reason |
| --- | --- |
| Copy the source's rows into the fork | breaks shared row identity; multiplies storage; pays for a deletion no path performs (§4) |
| One carrier with a persistence flag | a side turn would own jobs without a Session, or every checkpoint would get a Session row (§1) |
| Narrow a side turn's capability by removing tools from its request | shifts the cached prefix; policy at execution keeps the request byte-identical (§3) |
| Allow unsettled cut points and settle inherited calls from the source | couples recovery across Sessions; the settled rule deletes that path (§2, §4) |
| Resolve a fork point by wall-clock time | the same class #2393 removed from job matching; `context_seq` and row shape decide exactly (§2) |
| Merge a child's turns back into the source | two writers of one context and a second copy of rows (§9) |
| Store the parent-to-children relation in a new column | duplicates the metadata keys; an expression index serves the lookup (§5) |
| Let a side turn run `bash` with a synthetic call instance | a job needs a committed response to be recovered and settled; work that acts belongs in a fork Session (§1) |
