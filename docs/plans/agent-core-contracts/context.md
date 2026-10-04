# C-9 Context management

Frozen 2026-10-04 by owner decision. The `harness` owns the rules (`core/agent_core/harness/context.py`,
`projection.py`); the loop applies them before every model request (`core/agent_core/agent/loop.py`); the adapter
supplies the Session-specific parts (§9). Row shapes: [`transcript-rows.schema.json`](transcript-rows.schema.json);
events: [`agent-event.schema.json`](agent-event.schema.json).

Three tiers, cheapest first:

1. **Output governance at write time** (C-7): no tool result exceeds 2,000 lines / 50 KB.
2. **Clearing old tool results**, recorded as `context_edit` rows (§4). On by default.
3. **Checkpoint**, recorded as a `context_compaction` row, written by the model in a forked request (§5–§7).

Compaction and clearing only append rows; they never rewrite or delete one. A fork anchored before a checkpoint
projects the full original context (C-5 §4).

## 1. Limits

The hop capabilities Model Hub returns for the request (C-6) give the limits. One pure function,
`harness/context.budget(request, capabilities, transcript, anchor)`, computes `W, L_in, O, M, T, keep` and the
request's `est` (§2). It runs exactly once per request, on the final request after every rewrite, with the
capabilities of the route resolved for that request: conversation, checkpoint, and rolling requests, and the request
right after a checkpoint (§10, invariant 1). Nothing else computes or keeps these values, so a route change is picked
up at once; a smaller window than expected is handled by the overflow path (§8).

```text
W    = context_window                              (128,000 when unknown)
L_in = input_limit                                 (W when unknown)
O    = the request's max_tokens: for a conversation request max_output_tokens (8,192 when unknown), capped by the
       Agent's output budget; for a checkpoint request min(16,000, that)
M    = max(8,000, ceil(0.03 * W))
T    = min(L_in - O - M, floor(0.9 * W))           the compaction threshold
keep = min(20,000, floor(0.25 * T))                the verbatim tail of a normal checkpoint
```

A conversation request **fits** when `est + O + M <= L_in`, where `est` is its estimate (§2); `M` absorbs the
estimate's error. A request **can fit** when `est + O <= L_in`. A checkpoint request (§6) is sent only when it can
fit: at `est = T` it uses a few hundred tokens of `M` for the checkpoint request, and the provider remains the judge.

## 2. Token estimate

`tokens(x) = ceil(utf8_bytes(x) / 4)`: characters / 4 undercounts Chinese by about 65%, UTF-8 bytes / 4 is within 6%.
A message counts its text, its thinking text and signature (replayed reasoning payloads), each tool call's name,
JSON arguments, and signature, and 1,600 tokens per image.

The **anchor** is the latest response in the context that neither failed nor was aborted, reports non-zero usage,
and records the request it answered on its row (`ModelResponse.request.tokens`: `tokens()` of that whole request as
sent). It is rebuilt from the rows, so it holds across a new Agent and a new Turn.

```text
est = usage(R) + tokens(this request) - R.request.tokens - tokens(R)       while the anchor R holds
    = tokens(this request)                                                 otherwise
usage(R) = input_tokens + cache_read_tokens + cache_write_tokens + output_tokens
```

The anchor holds while (a) this request goes to the route that answered it (`R.origin` equals the request
endpoint's origin, because tokenizers differ), and (b) the transcript up to `R` is unchanged: the messages the rows
put before `R` now are the ones they put before it when `R`'s request was built, and `R` follows them. A checkpoint,
or an edit of a result before `R`, breaks (b). A changed system prompt, tool set, rehydrated state, or hook rewrite
does not invalidate the anchor: the difference between this request's `tokens()` and `R.request.tokens` carries it.
So does anything after `R`, an edit there included.

## 3. Before every model request

Every conversation request goes through one pipeline (§10, invariant 1): the projection, then the user's
`before_model` hooks, then `budget()` on that final request, then the C-9 stage below, then the provider. A stage
step that changes the context rebuilds the request from the top, hooks included; the request sent is exactly the one
last budgeted. The stage, inside the tool loop and before a retry alike:

1. **Stop** (§8 d) when even the checkpoint and the last unit cannot fit: `est` minus the rest of the transcript,
   plus `O`, exceeds `L_in`. The provider never sees the request.
2. **Clear** (§4) when the provider cache is cold (no model request for longer than the cache TTL, 300 s by
   default) or `est >= 0.8 * T`.
3. **Checkpoint** when `est >= T`, auto-compaction is not paused (§10), and there is something to summarize, at most
   once per model request: a normal checkpoint (§6, reason `threshold`) when the forked request can fit, otherwise
   the overflow ladder from (b) (§8, reason `overflow`). A checkpoint request that overflows continues at (b).
4. **Ladder** (§8) when the request does not fit. With no checkpoint request left to try (one failed for this
   request, or auto-compaction is paused), a request that can fit is sent and the provider judges.

A request the provider rejects as overflow (`ProviderError.kind == "overflow"`, classified by
`ai/errors.is_overflow_message`, HTTP 413, and `context_length_exceeded`) enters the overflow ladder (§8) and is retried.
A checkpoint turn's own requests (§6) are never checked: one compaction is in flight per Session.

## 4. Clearing old tool results

- Eligible: results of `read` and `bash` in the projected context (terminal screen snapshots join when they exist).
- Protected: results after the second-latest input (the last 2 user turns; with fewer than 2 inputs since the latest
  checkpoint, all of them), the newest 5 eligible results, skill loads (a result whose `details.skill` is set), and
  results already cleared. Results before the latest checkpoint are not in the context.
- Applied only when the candidates free at least 20,000 tokens, all at once, one `context_edit` row per result:
  `{"target_event_id": <tool_result row id>, "replacement": {"text": PLACEHOLDER}, "reason": "clear_old_tool_result"}`.
- `PLACEHOLDER` = `[Old tool result cleared to save context. Re-run the tool or re-read the file if you need it again.]`
- Projection keeps the call id, name, and error flag, and replaces the content with the placeholder. The latest
  edit of a target wins.

## 5. Cut point

The projected context after the latest checkpoint is a list of **units**: an input, or a response together with the
results of its tool calls. A cut falls only before a unit: before a user message, or before an assistant message
whose tool batch follows it; never at a tool result, so no call is separated from its result.

- **Normal** (threshold, manual, and the first overflow step): the tail is the longest run of whole units at the end
  whose tokens total at most `keep`, and at least the last unit. Everything before it is the head. An empty head
  means there is nothing to summarize.
- **Rolling** (§8 b): the cut nearest to half the tokens, moved earlier until the forked request over the head fits;
  never past the last unit.
- **Dropped** (§8 c): the cut nearest to half the tokens; never past the last unit.

`first_kept_seq` is the `context_seq` of the first kept unit. If that unit is not an input, the cut split a turn: the
text of the latest input in the head, or the previous checkpoint's current request when the head has none, is copied
verbatim into the checkpoint as `<current-request>` (images as `[image: name]`), with no model call.

## 6. Checkpoint turn

A checkpoint is written by a **fork** of the conversation, the only delivery path:

- The request uses the same hop, system prompt, tool definitions, tool choice, and reasoning settings as the
  conversation's next request, and its messages begin with the conversation's projected messages unchanged, so its
  prefix is byte-identical to the previous request's and a warm provider cache hits. Tool choice, thinking, and
  effort are never changed: Anthropic renders them into the prompt. `max_tokens = min(16,000, O)`.
- **Normal**: the prefix is the whole projected conversation. **Rolling**: the prefix is the projected context up to
  the cut, which is a prefix of the original request.
- One user message is appended: the checkpoint request (§11), with `Additional focus from the user: <focus>` for
  `/compact <focus>`.
- The turn runs through the same loop and the same request pipeline: transient retries, the user's `before_model`
  hooks, then `budget()` on the final request; a request that cannot fit is never sent and fails the attempt as an
  overflow. Its tool calls go through the tool pipeline (§10, invariant 2). Its responses and results are never
  committed to the context, so `after_model` and `after_tool` do not run and nothing is shown to the user.

**Tool policy** ("dreaming": cognition allowed, actuation blocked), a declarative table judged after the turn's
budget and the user's `before_tool` hooks, on the arguments they leave (invariant 2):

| Tool | Rule |
| --- | --- |
| `read` | allowed |
| `write`, `edit` | allowed only when the target's real path is inside this Session's scratch directory, `<state>/agent_core/scratch/<session_id>/`; a path that cannot be compared with it (another Windows drive, an invalid path) is outside. An allowed call runs against that real path and is pinned to it: `write` and `edit` publish only while the path still resolves there (`ToolContext.pinned_target`) |
| memory-read tools | reserved: allowed once they exist |
| `bash` and every other tool | denied, never executed |

- A denied call gets the error result `This is a checkpoint turn: tools that act outside your own scratch space are
  unavailable. Write the checkpoint now.`
- Every request of the turn is budgeted on the route resolved for it (§1): its `max_tokens` is that route's
  `min(16,000, O)`.
- The bound is the window, not `T`, the same way in threshold, manual, and rolling turns. Before each call,
  `room = L_in - est - min(16,000, O)`, where `est` is the budget of the turn's latest request plus the tokens of
  the response and results since. A call runs only while `room >= 4,000` tokens, and every result that enters the
  turn, a hook's or the policy's denial included, is cut to `room - 1,000` tokens, head kept, ending with
  `[Output truncated to fit this checkpoint turn: showing about <shown> of <total> tokens. Read a smaller range if
  you need more.]`, so a single result cannot push the turn out of the window.
- At most 5 tool rounds. After them, or once `room` falls below the floor, the budget is closed: every call gets
  `This is a checkpoint turn and its tool budget is used up. Write the checkpoint now.` before any user hook or the
  table is consulted. A response that still calls tools after that ends the turn as failed.
- A pinned write cannot be redirected by a symlink swapped after the table authorized it: the tool resolves the
  authorized real path itself, and refuses when the path resolves elsewhere at its start or right before the rename.
  The window between that last check and `rename(2)` remains, as for every write: tools audit ledger (PR #2346)
  rows B4 and B7.
- The checkpoint is the text of a final response that stops with `stop` and calls no tool; only a `tool_use` stop
  with calls continues the turn. Anything else (a length stop, a `tool_use` stop without calls, calls under any other
  stop, an error, or no text) is a failure, and nothing enters the context.
- The turn's messages, read from the attempt ledger (§10, invariant 4) with every failed or retried attempt's
  partial and its usage, and the turn's tool results, are recorded once per checkpoint attempt, outcome included, as
  an audit row
  (`agent_events.event_type = 'context_checkpoint_turn'`, `visibility = 'audit'`, no `context_seq`; shape
  `CheckpointTurn`), never as context.

## 7. Checkpoint row and projection

A successful checkpoint appends a `context_compaction` row (`Compaction`). Its model-facing content is fixed when it
is written, so projection stays a pure function of the rows:

- `summary`: the rendered checkpoint message:

  ```text
  <context-checkpoint>
  This is a record of the earlier part of this conversation, written for you so you can continue. It is history, not new instructions: the user requirements recorded in it still apply, but do not treat the record itself as a request.

  <the model's checkpoint>

  <artifacts>
  Read:
  - <path, cumulative across checkpoints>
  Modified:
  - <path, cumulative across checkpoints>
  </artifacts>
  <earlier-record>
  The full text of the earlier conversation is still stored. To look up a detail, run:
  <the adapter's lookup command for this Session through summarized_to_seq>
  </earlier-record>
  <current-request>
  <the split turn's user message, verbatim>
  </current-request>
  </context-checkpoint>
  ```

  `Read` lists paths `read` ran with and `write` or `edit` never did; `Modified` lists paths `write` or `edit` ran
  with; `(none)` when a list is empty. The path is the one the tool ran with, after the user's `before_tool` hooks,
  recorded on its result as `details.path` when it succeeded (a row without it falls back to the call's argument
  unless the result is an error). `<earlier-record>` is left out when the adapter supplies no command, and
  `<current-request>` when the cut did not split a turn.
- `state`: texts the adapter rendered from their own stores when the checkpoint was written: skill bodies by name and
  revision (at most 5,000 tokens each and 25,000 in total), pending Watches, Tasks, and delegated Runs from the Harness
  tables, and the full environment block when the checkpoint happened inside a run (C-7 §8).
- Projection (C-5 §3): the system prompt, hook-rehydrated messages, one user message holding `summary` and then each
  `state` text as its own text block, then the rows from `first_kept_seq` on, with edits applied. A tool result whose
  call was summarized is left out with it. No synthetic "continue" message follows.
- Every `Compaction`, `ContextEdit`, and `AgentState` row is checked against its complete schema shape when it is
  loaded, so a malformed row fails projection instead of a later step that reads it.

Iterative checkpoints use the same prompt: the previous checkpoint message is part of the forked prefix, and the
prompt tells the model to carry forward what still matters. File lists and loaded skills accumulate mechanically.

## 8. Overflow ladder

Bounded per model request. Each step makes the next request smaller; the ladder takes at least one step and stops
once the request fits:

- (a) **Normal checkpoint**, unless one already ran for this request, there is nothing to summarize, or the whole
  context cannot fit a forked request. A checkpoint request the provider rejects as overflow moves on to (b).
- (b) **Rolling**: fork-summarize the largest safe prefix that can fit (§5), so the context becomes checkpoint + the
  rest verbatim; roll again while the request still does not fit, at most 2 rolls per request.
- (c) **Dropped**: when no checkpoint request can help (one failed for this request other than by overflow, a rolling
  one failed, no rolling prefix can fit, the rolls are used up, or auto-compaction is paused), no model is called:
  the earliest part (§5) moves out of the context. The row keeps the previous checkpoint's text and the
  `<earlier-record>` pointer; `checkpoint` is empty when there was none, and the message then has no framing text.
  Repeated while the request still does not fit.
- (d) **Stop**: when even the last unit (the current request, carried as `<current-request>`, plus the latest tool
  batch) with the checkpoint cannot fit (checked in the stage, §3, before provider admission), when nothing more can
  move out of a request the provider refused, or after 4 provider overflows of one request, the run ends
  `context_exhausted` and the `context_exhausted` event says what fills the context. No model is called for a
  context that cannot fit.

A conversation attempt the provider refused, or that is retried, keeps its usage-only partial as a non-final response
row before the request goes back through the pipeline, so its billed tokens are recorded once.

With a provider that always overflows, one request makes at most two checkpoint model calls, then drops
mechanically, and stops after its fourth overflow.

## 9. What the adapter supplies

The loop takes a `ContextConfig` (`harness/context.py`); without one, no context management runs and an overflow
ends the run `context_exhausted`, as in P1. The adapter supplies:

- `scratch_dir`: this Session's scratch directory; without it, the policy denies every write.
- `ContextHost.earlier_record(session_id, through_seq)`: the lookup command, a `vibe data query` SQL naming every
  Session whose rows the context holds and the last summarized `context_seq`.
- `ContextHost.render_state(StateRequest)`: the `state` texts (§7) for the skills the summarized rows loaded and
  whether the checkpoint happened inside a run.
- `TranscriptStore.append_checkpoint_turn(session_id, payload)`: the audit row (§6);
  `append_payloads(session_id, entries)`: several payload rows in one transaction (invariant 3);
  `append_response(..., request=...)`: `ModelResponse.request` for the anchor (§2).
- `Agent.compact(turn_id=..., focus=...)` behind `/compact [focus]` on every surface, and the pause notice (§10)
  through `vibe/i18n`.

## 10. Guards

- **One compaction in flight per Session.** A checkpoint turn never starts another; `compact()` and `run()` refuse
  each other on one Agent; across Agent instances the adapter's Session writer lock serializes them.
- **Pause.** Each failed checkpoint request counts as a failure; a successful one resets the count. A successful
  normal checkpoint whose result is still at least `0.75 * T` counts as ineffective; an effective one resets that
  count. After 3 consecutive failures or 3 ineffective checkpoints, auto-compaction pauses for the Session: threshold
  checkpoints stop, and the overflow ladder skips straight to (c). The `compaction_paused` event fires once, at the
  transition, and the adapter tells the user. The counters and the pause are durable loop state, stored beside the
  hook state in `agent_state` rows (`AgentState.context`), so they survive restarts and forks; projection takes them
  from the latest `agent_state` row that carries them.
- **Manual `/compact [focus]`** clears the pause and both counters, then runs a normal checkpoint with reason
  `manual`; its own outcome counts as above. When everything is within the kept tail, it emits `compaction_skipped`
  and ends `completed`, and the adapter replies briefly that there is nothing to compact yet.

**Ordering and ownership invariants.** Each has one test in `tests/agent_core/agent/test_compaction.py` (route:
`test_context.py`) that fails when its order or owner is broken.

1. **One request pipeline.** Projection, then the user's `before_model` hooks, then `budget()` exactly once on that
   final request, then the C-9 stage, then the provider. Nothing changes a request after it is budgeted; a stage step
   that changes the context rebuilds the request from the top. The single-unit stop (§8 d) is in the stage, before
   provider admission. Checkpoint requests take the same pipeline. Under C-9 a `before_model` hook (C-3 §3) may
   append messages and change the system prompt or tool definitions, and nothing else: routing owns the model (the
   endpoint's protocol, base URL, provider, and model stay as resolved), and the projected messages are an immutable
   prefix (no prepend, removal, reorder, or rewrite, which would also break the provider cache prefix and the fork's
   byte-identical prefix). A violation ends the run with a `HookContractError`. So moving history out shrinks the
   request by exactly that history; appended hook content is overhead, budgeted like any other content, and an
   overflow that only that overhead causes goes to (d) with the `transient` part, dropping no history.
2. **One tool pipeline** in a checkpoint turn: the turn's budget (once closed, `BUDGET_USED` with no hook
   consulted), then the user's `before_tool` (`Deny`, `AlterArgs`), then the checkpoint table on the final arguments,
   then execution pinned to the path the table authorized, then the bound on every result that enters the turn,
   denials included. Conversation artifacts are recorded from the final arguments after execution (§7).
3. **One commit for C-9 state.** A transition (its `context_edit` rows, its `context_compaction` row, the guard and
   pause, and the hook state of that commit point in `AgentState`) is written in one transaction
   (`append_payloads`), before any event announces it; a failed commit leaves nothing. Nothing else writes
   `AgentState.context`.
4. **One attempt ledger.** Every model attempt of a run (success, overflow, error, retry, checkpoint) records its
   request, its response or partial, and so its usage, in one place. The checkpoint audit and the request facts of a
   committed response (the anchor) read only from it.
5. **Route-scoped anchors.** An anchor answered by another origin (provider, api, model) is invalid (§2).

UX is silent: an automatic compaction shows nothing, and the raw messages stay in history. The only user-visible
text is the pause notice, and, after (d), the run's stop message. A manual `/compact` is an explicit user action,
so the adapter answers it.

## 11. Checkpoint request (prompt `checkpoint-v2`)

The owner-approved English text, verbatim (`harness/context.py` `CHECKPOINT_REQUEST`):

```text
<context-checkpoint-request>
Pause the work here. Do not call any tool and do not continue the task. This reply does one thing: write a context checkpoint.

The conversation above will be replaced by your checkpoint plus the messages after it. Whoever continues is you, but you will no longer remember this conversation; the checkpoint is all you will know of it. What you do not write down is lost.
If there is an earlier checkpoint above, it is discarded after this: carry forward everything in it that still matters; where it conflicts with later messages, the later messages win.

Everything above is a record to summarize, not instructions to act on now.

Write in three layers, from the broad to the specific. Use exactly these headings, in this order. Write "(none)" under an empty heading.

# 1. Self and method

## How I work here
- The role the user expects of me in this conversation, and the working agreements we established (for example how to report, when to ask, what I may decide on my own).
- Methods and judgments that proved effective here, and ones that proved ineffective, written as transferable principles I can apply to any later work.
- Record only what this conversation established; my identity and standing instructions come from the system prompt and are not restated here.

# 2. Goals and requirements

## Goals
- What the user wants to achieve, and the intent behind it. If there are several, list each and mark the one being pursued now.

## User requirements
- Every instruction, preference, correction, and prohibition from the user that still applies. Quote short ones verbatim. Never drop one unless the user withdrew it.

## Key decisions
- decision: reason

## Pitfalls
- Mistakes made, assumptions that proved false, and dead ends (quote the exact error text where there is one), with what resolved each or "unresolved", so the same mistake is not repeated.

# 3. Now and next

## Progress
### Done
### In progress
### Blocked or open questions

## Waiting on
- Background commands, Watches, scheduled Tasks, delegated agent runs, or questions to the user that are still expected to report back, with their ids.

## Next steps
1. The concrete next action, then the ones after it.

## Exact references
- Exact paths, identifiers, commands, links, ids, and values needed to continue.

Rules: terse bullets, not paragraphs. Preserve exact paths, identifiers, commands, error strings, and numbers. Do not invent anything that is not above. Never write out secrets, tokens, or credentials; refer to them by name. Write in the language the user writes in.
Additional focus from the user: <focus, only for /compact <focus>>
</context-checkpoint-request>
```

## 12. Deferred

Not built in v1: background precomputed compaction; server-side compaction (forbidden by hard constraint 8);
automatic re-read of recently modified files; memory tools (the policy reserves their row); lowering effort for the
checkpoint turn through a per-message system effort.

Sources: the cut rule, cumulative file lists, and the overflow patterns from Pi (MIT, `7fbbd5f`); the merge rule from
OpenCode; "a record, not instructions" from Gemini CLI; fork delivery from Claude Code; the waiting-on section, the
earlier-record pointer, and the rolling ladder are Avibe's (research: evaluation §4).
