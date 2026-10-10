# C-10 Fork

Status: **frozen** 2026-10-10 by owner decision (§18). Every `file:line` was read on `avibe-agent` at `ce169436c`. The
delta in §16 is applied to the contracts it touches. A change now needs orchestrator approval and lands here first
(README).

A fork is an execution context whose model-visible messages begin with a source context's projection at a **fork
point**, and which then diverges on its own. Fork is the base other features build on: C-9's checkpoints today, and
later an Agent copying itself into a team, managing its own context, and consolidating memory.

## 0. Example

Session P runs on Vibey and has 40 context rows. Its fourth Turn ended with Vibey's reply at `context_seq` 23. Its fifth
Turn began with the user's input at 24 and is in the middle of a tool batch. The user clicks Fork.

1. The reservation resolves the fork point. P has a live Turn whose first input is row 24, so the cut is the largest
   settled `context_seq` before 24: 23, the end of the previous ended Turn. No tool call at or before 23 lacks its
   result, so the cut is legal. It writes Session C with `fork_source_session_id = P` and
   `fork_source_context_seq = 23`, and copies no row. Had the fourth Turn been stopped or failed, its open calls would
   have been settled by recovery (T2), and the cut would be its last settled row.
2. C's first Turn loads P's rows with `context_seq <= 23` and projects them. The result is what P's model saw at 23:
   checkpoints and edits written up to 23 apply, later ones do not. Nothing of the fifth Turn reaches C: not its input,
   its tool calls, or its intermediate replies. The user's message becomes C's row 24.
3. P commits 41, 42, and so on. P and C are different Sessions with different locks and `context_seq` spaces. C never
   sees what P writes after 23, and P never sees what C writes.
4. P's Watch `w1`, started at row 18, stays P's, and its follow-up goes to P. C's environment block lists only C's
   Watches. C's fork notice says that work started before the fork reports to the Session that started it.
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
| Consumers | C-9 checkpoints, memory consolidation, rewriting its own context | the user's Fork, `vibe agent run --fork-*`, later teammates and delegated exploration |

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
  Its projection of the inherited rows is the source's view at `as_of`. A user's fork always takes the latest point
  (below); an arbitrary settled point is an internal capability for system mechanisms, with no product surface
  (owner decision O-2, §18).
- **A side turn uses `as_of` = the caller's latest committed row.** That row is the run's own view. `units` selects
  a prefix of that view, which C-9's rolling checkpoint needs (`view.prefix(cut)`, `loop.py:1280-1282`). A side turn
  never forks an older view in v1: a side turn exists to reuse a byte prefix of the request just sent (§3).

**Why two coordinates.** A later checkpoint or edit changes how earlier rows project.

- The view at `as_of` is what the model saw then. A fork Session wants this view.
- A prefix of the current view is what the model sees now, cut short. C-9's rolling checkpoint wants this one.

The view at `as_of` also defines the prefix of a cut in a compacted region:

- If a checkpoint was written at or before `as_of`, the prefix is that checkpoint plus its kept tail.
- If a checkpoint was written after `as_of`, it did not exist then. The prefix is the rows as they were (A4).

### Resolving a fork Session's point

This replaces `resolve_fork_anchor_seq` and `context_bound` (`storage/agent_transcript.py:492-536`). Those match
`agent_events` rows to an anchor message by wall clock (`:528-534`); this rule uses only `context_seq`, row shape,
and the live Turn's first input.

| Request | `as_of` |
| --- | --- |
| a user's fork (the latest point), and the source has a live Turn | the largest settled `context_seq` before that Turn's first consumed input (of the whole context while the Turn has consumed none) |
| a user's fork, and the source has no live Turn | the largest settled `context_seq` of the source's context |
| a system mechanism's fork at a given `context_seq` `s` (internal only) | `s`, when `0 <= s <=` the source's last `context_seq` and `s` is settled; otherwise refused (`ForkPointError`) |

**The owner's rule for a user's fork.** The cut is the end of the previous **ended** Turn: its final reply, or, for a
Turn the user stopped or that failed, its last row once recovery has settled its calls (T2). The live Turn's input,
its tool calls, and its intermediate responses never reach the child. This holds by construction:

- The live Turn writes all of its rows after its first input, because the loop is the Session's single writer
  (C-5 §2), so every row before that input belongs to an ended Turn.
- The cut is fixed once, in the reservation's transaction, from committed rows. Rows that receive a `context_seq`
  later never move into or out of the prefix (C-5 §4).
- The cut is settled, so an ended Turn whose calls recovery has not settled yet contributes only up to its last
  settled row. The fork never waits on, or races with, a timing condition.

Other rules:

- **0 is the empty prefix.** A child with `fork_source_context_seq = 0` inherits no row, and its first own row is
  `context_seq` 1. `fork_link` already accepts 0 (`agent_transcript.py:677-678`), and `context_view` over no row is
  empty.
- **An internal point may lie in the source's inherited prefix.** The ancestry walk bounds every ancestor by it
  (`_ancestry`, `agent_transcript.py:682-698`), so the prefix is the same as a fork of the ancestor at that point.

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
- Its messages begin with the caller's latest request's messages, rehydrated messages included, through the cut,
  byte for byte.
- It narrows capability when a call executes, never in the tool list. Removing a denied tool from the request would
  shift every byte after it.

**A fork Session is cache-cold today.**

- Vibey's system prompt carries the Session's own id, plus a fork note (`core/system_prompt_injection.py:228-231`,
  `core/prompts/forked-session.md`). So a child's first request shares only the tool definitions with its source.
- For a user's fork, correct identity is worth one cold prefix.
- For N teammates forked from one large context, it is N cold copies of that context.
- The remedy is a Session-invariant system prompt, with the Session id moved into the environment block of the
  first input (C-7 §8). That is left to the teams design (§17).

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
  - `fork_source_message_id`: unchanged, the anchor message the reservation reads (for a live Turn, its first
    input, which the cut excludes); native backends trim at it
  - `fork_source_context_seq`: Vibey only
  - `fork_created_at`

  Phase 1 adds no new shape.
- **Source to children.** This is absent today. Lineage is recorded only child to parent, and the UI only shows the
  child's banner (`ui/src/components/workbench/ChatPage.tsx:4071-4083`). Retention and teams need the reverse
  lookup. When one of them is designed, it adds an expression index on
  `json_extract(metadata_json, '$.fork_source_session_id')` rather than a column, so the metadata keys stay the one
  record.
- **Side turns.** Each leaves one `fork_turn` audit row in the caller's Session, recording its point, purpose,
  policy, and outcome (§7). That row is a side turn's lineage.

## 6. Capability

**F6:** a fork never exceeds the authority it was created under. A side turn never has a capability its caller lacks.
A fork Session never has more than the requesting caller may select: its Agent is the source's, or one the caller has
selection authority for on the same backend. That Agent may hold tools the source's Agent did not; the bound is the
caller's authority, not the source's tool set.

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

**Fork Session.** Its tools are its Agent's, which `--agent` may override (`vibe/cli.py:1857`). The reservation
already checks that the caller has:

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
   - It is composed by the caller's request builder (`_built`, `loop.py:622-649`) from the caller's rehydrated
     messages (hook state, which are not rows; C-5 §3 step 6), then `prefix(caller, (latest, units))`, then `prompt`,
     then the turn so far, as `_fork` composes it today (`loop.py:1284-1302`).
   - It uses the caller's endpoint, system prompt, tool definitions, and reasoning settings. `max_tokens` comes from
     the spec, per route.
   - The request actually composed is budgeted. One that cannot fit is never sent, and the turn fails as an overflow.
   - The dry run that decides whether to start (`side_turn_fits`; C-9's `_fork_fits`, `:1304-1309`) composes the same
     request, rehydrated messages included (C-9 §10, invariant 1).
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

- **Reservation.** `reserve_forked_session` (`session_fork.py:177`) keeps its signature and its callers. For `vibey`
  it resolves the user's latest point (§2) inside the reservation's transaction and writes the metadata (§5). Native
  backends keep their own latest-point fork and trimming (§11).
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
  - The child's Workbench is unchanged: its own rows and today's fork banner, which links to the source Session
    (`ChatPage.tsx:4071-4083`). The inherited prefix is not rendered in the child (owner decision O-3).
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
| Security | a fork exceeding the authority it was created under | F6 (§6) |
| False information | a child believing it owns its source's Watches or jobs; a side turn's messages leaking into context | the fork notice and the child-only environment block (§8); a side turn's messages are audit only (§7) |

**Runaway bounds for fork Sessions.**

- Today only the Harness's process-wide limit of 8 concurrent runs bounds them (`core/scheduled_tasks.py:3971`).
- Model Hub does not cap spend, by design (`core/handlers/model_hub/usage.py:18-24`).
- Delegation depth is recorded (`parent_run_id`, `vibe/cli.py:6898`) but not bounded.
- Plain delegation, `vibe agent run --agent`, recurses the same way.
- Owner decision O-4: no new limit. Agents keep forking themselves through `vibe agent run --fork-self` with no
  depth or descendant bound.

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
| `core/agent_core/harness` | `ForkPoint`, `settled`, `latest_cut`, `fork_point`, `fork_prefix` (pure, §2) | add them in `harness/fork.py`; `fork_prefix` is `context_view` plus `prefix` |
| `core/agent_core/agent` | side turn: `SideTurn`, `ForkPolicy`, `DREAMING`, `Agent.side_turn`, `Agent.side_turn_fits` | extract them from C-9 (§12) |
| `storage/agent_transcript.py` | storage by reference, ancestry, `resolve_fork_point` (SQL around the harness rule) | replace `resolve_fork_anchor_seq` and `context_bound` |
| `modules/agents/vibey` | the fork notice on the first input; T2 without the inherited branch | as listed |
| `core/services/session_fork.py` | the reservation for every backend | for `vibey`, call `resolve_fork_point`; no new parameter |

### Backend support

| Backend | Fork | Phase |
| --- | --- | --- |
| `vibey` | by reference, at the user's latest point (§2); arbitrary settled points internally | 1 |
| `codex` | unchanged: `thread/fork` (`modules/agents/codex/agent.py:3316-3421`), with `lastTurnId` to trim a live Turn (`:3393-3408`) | none |
| `claude` | unchanged: `resume` with `fork_session` (`core/handlers/session_handler.py:1558-1563`, `:1730-1731`) | none |
| `opencode` | unchanged: `POST /session/{id}/fork`, with `messageID` to trim (`modules/agents/opencode/server.py:818-837`) | none |

Point-in-time fork for native backends is cancelled (owner decision, 2026-10-10): they keep forking at the latest
point, which they already support.

### Surfaces

There is no new surface (owner decisions O-2 and O-3). The existing ones keep their shape and reach the cut rule
through `reserve_forked_session`:

- **Workbench.** The session menu's Fork (`ui/src/components/workbench/useSessionActions.tsx:141-161`) and "Ask in a
  new session" (`ChatPage.tsx:487-503`) post `{}` to `POST /api/sessions/<id>/fork` (`vibe/ui_server.py:9377-9400`).
- **CLI.** `vibe agent run --fork-session <id>` and `--fork-self` (`vibe/cli.py:6592-6640`).
- **Not built:** CLI `--at`, an HTTP `at_message_id`, a Web "fork from here", an IM command, and a fork tool for
  Agents. Agents fork themselves with `vibe agent run --fork-self`, and the result returns by callback.

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
  - Needs elsewhere, left to the teams design: the descendant lookup (§5) and a Session-invariant system prompt, so
    that N forks of one large context hit the cache (§3). No depth or descendant bound (O-4).
- **Self context management.**
  - Explore and come back: a background fork Session with full tools, because exploring needs `bash`. Its reply
    returns as a message.
  - Go back: a system mechanism forks the Session at an earlier settled point through the internal API (§2, §15)
    and continues there. Phase 1 provides the API; there is no user or Agent surface for it.
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
| F3 | Every fork point is settled; a fork never inherits an open call, and never settles or looks up a call it did not make (settled calls are inherited as history, F1) | harness, storage | a user's fork while the live Turn is mid tool batch gets exactly the previous ended Turn's prefix; a stopped and a failed previous Turn are legal cuts; nothing of the live Turn (input, calls, responses) reaches the child; a job handed to a Watch does not block a cut; the internal API refuses an unsettled or out-of-range point and accepts 0 |
| F4 | A fork Session owns nothing an ancestor started: jobs, Watches, Tasks, runs, scratch stay with the Session that started them. A side turn starts nothing: its calls have no call instance | vibey, service, agent | a child of a source with a live job Watch neither lists it nor receives its follow-up, and its first input carries the notice; a side turn's `bash` call starts no job even when a policy allowed it |
| F5 | A fork Session never writes a row of its source. A side turn writes only its audit row and, after the reply, what its fold commits | agent, storage | the source's rows before and after a child's Turns; the caller's rows after a failed and after a successful side turn |
| F6 | A side turn has no capability its caller lacks; a fork Session runs an Agent the requesting caller may select, on the source's backend | agent, service | a side turn's call to every tool its policy does not allow is denied and never runs; a reservation without editor on the source, chat in the destination, or selection authority for the Agent (inherited or `--agent`) is refused, as is an Agent on another backend |
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
    code: Literal["unsettled", "out_of_range"]       # internal: no product surface reaches it

def settled(entries: Sequence[ContextEntry], as_of: int) -> bool: ...
def latest_cut(entries: Sequence[ContextEntry], *, live_input_seq: Optional[int]) -> int: ...   # a user's fork
def fork_point(entries: Sequence[ContextEntry], as_of: int) -> ForkPoint: ...   # internal; raises ForkPointError
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
    units: Optional[int]                                   # the caller's current view: all of it, or its first units;
                                                           # the request puts the caller's rehydrated messages first
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
def resolve_fork_point(conn: Connection, source_session_id: str, *, as_of: Optional[int] = None) -> int: ...
# None: the user's latest point (§2), from the source's rows and its live Turn's first consumed input.
# An int: an internal point, checked by fork_point.

# core/services/session_fork.py: reserve_forked_session keeps its signature;
# for vibey it calls resolve_fork_point(conn, source_session_id)
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

Applied in the freeze PR. Built from a search of `docs/plans` for every fork term and every identifier C-10 renames
or replaces (`fork`, `anchor_seq`, `snapshot`, `dreaming`, `checkpoint_turn`, `CheckpointTurn`, `append_audit`). Rows
not listed keep their meaning: their "fork" is the side turn or the fork Session as C-10 defines them. Line numbers
are those before the freeze PR.

| File | Location | Change |
| --- | --- | --- |
| `transcript.md` | §1, lines 30-31 | the audit kind `context_checkpoint_turn` (`CheckpointTurn`) becomes `fork_turn` (`ForkTurn`, C-10 §7) |
| `transcript.md` | §4, lines 87-90 | `anchor_seq` resolution becomes C-10 §2's fork point: settled, the user's latest point, 0 for the empty prefix, no clock |
| `transcript.md` | §5, lines 101-102 | points to F8 |
| `transcript-rows.schema.json` | `CheckpointTurn`, lines 348-404 | becomes `ForkTurn` (§15); the `append_audit` kind in its description becomes `fork_turn` |
| `context.md` | §6, lines 152-216 | the checkpoint is a side turn (C-10 §7); the dreaming table stays here as the value of `DREAMING`; the budget, room, rounds, and bound bullets point to C-10 §7; the audit becomes `fork_turn` with `purpose: checkpoint` and `detail: {reason, mode}` |
| `context.md` | §9, lines 355-356 | the `append_audit` kinds become `fork_turn` and `attempt` |
| `context.md` | §10, invariant 1 (line 395) | "the fork the stage would send first" is `side_turn_fits`, rehydrated messages included |
| `context.md` | §10, invariant 2 (line 404) | points to C-10 §7 step 3 |
| `context.md` | §10, invariant 4 (line 421) | the `CheckpointTurn` row becomes the `fork_turn` audit (F7) |
| `context.md` | §12, lines 494-496 | the reserved memory row points to C-10 §13 |
| `loop-control.md` | §1, lines 8-20 | the `Agent` surface adds `side_turn_fits` and `side_turn` (C-10 §15) |
| `loop-control.md` | §7, lines 111-115 | "Snapshot and fork" points to C-10; `snapshot()` stays the `after_run` outcome's state |
| plan `avibe-agent-core.md` | §4, line 79 | `agent/` holds the side turn, not a "fork snapshot" |
| plan | §5 table, lines 104 and 106 | C-3's "snapshot and fork" and C-5's "fork by reference" point to C-10 |
| plan | §5.1, lines 165-167 | the child's context is the source's rows up to the fork point (C-10 §2) |
| plan | §5.2, lines 195-199 | checkpoint delivery is a side turn under the `dreaming` policy |
| plan | §6 A10, lines 330-332 | a checkpoint turn's request is a side turn's (C-10 §7) |
| `README.md`, plan §5 | the C-10 rows | added as a draft by #2397; marked frozen |

## 17. Phases

| Phase | Scope | Acceptance (properties) |
| --- | --- | --- |
| 0 | this contract; owner decisions; the §16 delta; freeze | done 2026-10-10 |
| 1a | the fork point for Vibey: `harness/fork.py` (`ForkPoint`, `settled`, `latest_cut`, `fork_point`, `fork_prefix`), `resolve_fork_point` (no clock); the existing session fork wired to it for `vibey`, trimming the live Turn; the fork notice; T2's inherited branch removed; the internal API for an arbitrary settled point | F1, F3, F4, F6 (reservation), F8 pin; an E2E on a local dev instance with a throwaway home: fork a Vibey Session while it runs a long tool call, check the child's context, and check that the parent continues unaffected |
| 1b | C-9 on the side turn, as a pure refactor: `ForkPolicy` and `DREAMING`, `Agent.side_turn`, the `fork_turn` audit | F2, F5, F6 (policy), F7; the C-9 suite passes with only the audit kind renamed |
| later | teams: the descendant lookup, a Session-invariant system prompt, an Agent fork tool, side turns while idle | a separate design |

Point-in-time fork for native backends (the former Phase 3) and the Harness bound are cancelled by owner decision.

Files each lane touches, from a search of the code, tests, and prompts for the identifiers it changes
(`resolve_fork_anchor_seq`, `context_bound`, `fork_source_context_seq`, `call_instance_result`, `checkpoint_turn`,
`CheckpointTurn`, `CheckpointPolicy`, `CHECKPOINT_TOOL_*`, `_fork_fits`):

| Lane | Code | Tests | Prompts |
| --- | --- | --- | --- |
| 1a | `core/agent_core/harness/fork.py` (new); `storage/agent_transcript.py` (fork resolution); `core/services/session_fork.py`; `modules/agents/vibey/agent.py` (T2's inherited branch, the fork notice) | `tests/agent_core/harness/` (the pure rule), `tests/test_agent_transcript.py`, `tests/test_transcript_store_contract.py`, `tests/test_vibey_agent.py`, `tests/test_vibey_agent_context.py`, `tests/test_session_fork.py` | the fork notice's text |
| 1b | `core/agent_core/agent/loop.py`; `core/agent_core/agent/checkpoint.py` (becomes `fork.py`); `core/agent_core/harness/context.py` (the `CHECKPOINT_TOOL_*` constants); `core/agent_core/harness/store.py` (the audit kind); `storage/agent_transcript.py` (the audit map, lines 94-95); `modules/agents/vibey/store.py` (the audit kind) | `tests/agent_core/fakes.py`, `tests/agent_core/agent/test_compaction.py`, `tests/agent_core/agent/test_context.py`, `tests/test_agent_transcript.py`, `tests/test_transcript_store_contract.py` | none |

1b follows 1a; they are not parallel lanes. Both edit `storage/agent_transcript.py` and its two test suites, and 1b
records the `ForkPoint` that 1a adds in `harness/fork.py`. 1b reopens review on C-9's code, the most-reviewed code on
`avibe-agent`. Keeping 1b a pure refactor, with C-9's tests unchanged, keeps that review to the move itself.

## 18. Owner decisions

Decided 2026-10-10.

| ID | Decision |
| --- | --- |
| O-1 | Yes: one primitive (the fork point) with two carriers separated by identity. A side turn can do nothing that outlives its turn; a fork Session does everything else. C-9's checkpoint becomes the first side turn. "It must be implemented this way." |
| O-2 | Point-in-time fork is internal only. Forking at an arbitrary settled point is an engine capability for system mechanisms (C-9, later memory and self context management), with no CLI, HTTP, or Web surface. A user's fork always takes the latest point: the end of the previous ended Turn, by construction (§2). |
| O-3 | No new Web interaction. The existing Fork action stays and follows the cut rule. No banner change, no inherited-history rendering, no edit-and-resend, no `design.pen` frame. |
| O-4 | No new limits. Agents keep forking themselves through `vibe agent run --fork-self`, with no depth or descendant bound. |

Also decided: point-in-time fork for native backends is cancelled, and teams stay a later design.

Decided in the design lane, all reversible:

- A user's fork of a Vibey Session trims a live Turn (§2), which O-2's rule confirms.
- The audit kind is renamed `fork_turn` (§7).
- Scratch is not forked (§4).
- Making the system prompt Session-invariant is left to the teams design (§3).

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
| A user-facing point-in-time fork (CLI `--at`, HTTP `at_message_id`, Web "fork from here") | owner decision O-2: the arbitrary point is an internal capability |
| A depth or descendant bound on Agent self-forks | owner decision O-4 |
