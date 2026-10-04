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

The hop capabilities Model Hub returns for the route resolved for the request (C-6) give the limits. `O` is set
when the request is built, as its `max_tokens` (`output_tokens()`, `checkpoint_max_tokens()`). One pure function,
`harness/context.budget(request, capabilities, transcript, anchor)`, derives `W, L_in, M, T, keep` and the request's
`est` (§2) and reads `O` from the request, once per request (§10, invariant 1). No state keeps these values between
requests, so a route change is picked up at once; a smaller window than expected is handled by the overflow path
(§8). Every fit decision is `budget()` on a request actually composed (§10, invariant 1), including the two the
stage only considers: the fork it would send first, and the minimal request of the stop check.

```text
W    = context_window                              (128,000 when unknown)
L_in = input_limit                                 (W when unknown)
O    = the request's max_tokens: for a conversation request max_output_tokens (8,192 when unknown), capped by the
       Agent's output budget; for a checkpoint request min(16,000, that)
M    = max(8,000, ceil(0.03 * W))
T    = min(L_in - O - M, floor(0.9 * W))           the compaction threshold
keep = min(20,000, floor(0.25 * T))                the verbatim tail of a normal checkpoint
```

`O` is what the request asks for, not the most the route could produce. Model Hub model definitions are seeded from
models.dev, where many models list an output maximum as large as the window: in the snapshot checked on 2026-10-04,
1,337 of 8,150 entries with both limits have `max_output_tokens + M >= context_window` (so `T <= 0` and nothing could
be sent), and 296 more would leave `T < 0.25 * W`. The adapter therefore asks for
`min(max_output_tokens, floor(W / 4))` on every hop it resolves (a retry's fallback included), the maximum 8,192 when
unknown, so the output never takes more than a quarter of any window; the limits themselves still come from Model
Hub. Below a window of about 12,000 tokens `T <= 0` (the fixed `M`), so C-9 checkpoints on every request until the
pause (§10), as Pi's fixed reserve does.

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
or an edit of a result before `R`, breaks (b). A changed system prompt, tool set, or rehydrated state
does not invalidate the anchor: the difference between this request's `tokens()` and `R.request.tokens` carries it.
So does anything after `R`, an edit there included.

## 3. Before every model request

Every conversation request takes the request pipeline (§10, invariant 1). Its C-9 stage, inside the tool loop and
before a retry alike:

1. **Stop** (§8 d) when the request cannot fit and neither can its minimal request: the request the drop (§8 c)
   would leave with everything but the last unit moved out (the drop's own row, with its `state` and, when the cut
   splits a turn, its `<current-request>` copy, §5), budgeted without the anchor (its history would be gone). With
   fewer than two units nothing can move out, and the minimal request is the request itself. The provider never
   sees the request.
2. **Clear** (§4) when the provider cache is cold (no model request for longer than the cache TTL, 300 s by
   default; after a restart, measured from the latest response row's `created_at`) or `est >= 0.8 * T`.
3. **Checkpoint** when `est >= T`, auto-compaction is not paused (§10), and there is something to summarize, at most
   once per model request: a normal checkpoint (§6, reason `threshold`) when the fork it would send can fit,
   otherwise the overflow ladder from (b) (§8, reason `overflow`). A checkpoint request that overflows continues at
   (b).
4. **Ladder** (§8) when the request does not fit. With no checkpoint request left to try (one failed for this
   request other than by overflow, or auto-compaction is paused), a request that can fit is sent and the provider
   judges. While auto-compaction is paused nothing is compacted at all: a request that cannot fit, or that the
   provider refuses as overflow, ends the run `context_exhausted` (§10).

A request the provider rejects as overflow (`ProviderError.kind == "overflow"`, classified by
`ai/errors.is_overflow_message`, HTTP 413, and `context_length_exceeded`) before anything was streamed, and with no
content in its partial, enters the overflow ladder (§8) and is rebuilt and sent again. Any other overflow is never
retried, even when the adapter reports no partial, since the user may have seen what was streamed: a partial with
content is committed as a non-final response, and the run ends `context_exhausted`. A checkpoint turn's own
requests (§6) never run this stage (no clearing, checkpoint, or ladder; one compaction is in flight per Session);
each is sent only when it can fit.

## 4. Clearing old tool results

- Eligible: results of `read` and `bash` in the projected context (terminal screen snapshots join when they exist).
- Protected: results after the second-latest input (the last 2 user turns; with fewer than 2 inputs since the latest
  checkpoint, all of them), the newest 5 eligible results, skill loads (a result whose `details.skills` is set), and
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

- **Normal** (threshold, and the first overflow step): the tail is the longest run of whole units at the end
  whose tokens total at most `keep`, and at least the last unit. Everything before it is the head. An empty head
  means there is nothing to summarize.
- **Rolling** (§8 b): the cut nearest to half the tokens, moved earlier until the forked request over the head can fit
  (§1); never past the last unit.
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
- One user message is appended: the checkpoint request (§11).
- The turn's requests take the request pipeline (§10, invariant 1; a request that cannot fit is never sent and
  fails the attempt as an overflow), its responses the admission (invariant 6), and its tool calls the tool pipeline
  (invariant 2). Its responses and results are never committed to the context, and nothing is shown to the user.

**Tool policy** ("dreaming": cognition allowed, actuation blocked), a declarative table judged after the turn's
budget (invariant 2):

| Tool | Rule |
| --- | --- |
| `read` | allowed |
| `write`, `edit` | allowed only for a single file name directly inside this Session's scratch root, `<state>/agent_core/scratch/<session_id>/` (the path's directory is the root; a name with a separator, `.`, or `..` is denied). Scratch is flat and is reached only through a directory descriptor: when the turn starts, it creates the root and opens it once (`O_DIRECTORY \| O_NOFOLLOW`, `fstat` a real directory; a symlink authorizes nothing). Every read, temporary file (`O_CREAT \| O_EXCL \| O_NOFOLLOW`), and publication (`os.replace` with `src_dir_fd`/`dst_dir_fd`) is relative to that descriptor, so renaming or swapping the root, or a name in it, after it was opened redirects nothing; a name is never followed, and one that is not a regular file when the call runs is refused. Where the platform cannot open and rename relative to a descriptor (Windows), scratch writes are denied and `read` stays allowed. Each scratch call runs as joined work an abort waits for, so the descriptor closes only after its worker thread is done |
| memory-read tools | reserved: allowed once they exist |
| `bash` and every other tool | denied, never executed |

- A denied call gets the error result `This is a checkpoint turn: tools that act outside your own scratch space are
  unavailable. Write the checkpoint now.`
- Every request of the turn is budgeted on the route resolved for it (§1): its `max_tokens` is that route's
  `min(16,000, O)`.
- The bound is the window, not `T`, the same way in threshold and rolling turns. Before each call,
  `room = L_in - est - min(16,000, O) - reserved`, where `est` is the budget of the turn's latest request plus the
  tokens of the response and results since, and `reserved` is one fixed-text result (the longer of the two below)
  for each later call of the same response, so every result of the batch is accounted for before it exists. This is
  the one bound that extends the latest request's budget by arithmetic (§10, invariant 1). A call runs only while
  `room >= 4,000` tokens, and the result of every call that runs (an unknown tool's error included) is cut to
  `room - 1,000` tokens, head kept, ending with
  `[Output truncated to fit this checkpoint turn: showing about <shown> of <total> tokens. Read a smaller range if
  you need more.]`, so a single result cannot push the turn out of the window. The policy's two fixed texts (the
  denial and the used-up budget) are a few dozen tokens and are never cut: a truncation note would invite another
  call.
- At most 5 tool rounds run. After them, or once `room` falls below the floor, the budget is closed: every call gets
  `This is a checkpoint turn and its tool budget is used up. Write the checkpoint now.` before the table is
  consulted, so the audit's `rounds` can count one more response, the one answered that way. A response that still
  calls tools after that ends the turn as failed.
- `write` and `edit` in a checkpoint turn keep the tools' arguments and success texts (C-7); their errors are their
  own, short and without the C-7 advice to use `bash`, which the turn may not call. `edit` reuses the tools'
  matching (`edit_diff`), BOM, and line-ending handling. The C-7 tools themselves are unchanged.
- The checkpoint is the text of a final response that stops with `stop` and calls no tool; only a `tool_use` stop
  with calls continues the turn. Anything else (a response refused at admission, a length stop, a `tool_use` stop
  without calls, calls under any other stop, an error, or no text) is a failure, and nothing enters the context;
  so is a checkpoint whose row the host cannot complete (§10, invariant 4).
- The turn's messages, read from the attempt ledger (§10, invariant 4) with every failed or retried attempt's
  partial and its usage, and the turn's tool results, are recorded once per checkpoint turn, on every exit but an
  abort (§10, invariant 4), outcome included, as an audit row
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
  - <path, cumulative across checkpoints, most recently touched first, at most 50>
  - and <N> more
  Modified:
  - <path, cumulative across checkpoints, most recently touched first, at most 50>
  - and <N> more
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

  `Read` lists paths of successful `read` calls that no `write` or `edit` touched; `Modified` lists paths of
  successful `write` or `edit` calls; `(none)` when a list is empty. Each path is cut in the middle to 160
  characters (its head and its file name stay). Each list keeps its 50 most recently touched paths and counts the
  ones pushed out (`files_read_more`, `files_modified_more`), in the row and in the text; the
  full history stays reachable through `<earlier-record>`. This is an Avibe deviation from Pi, which keeps every
  path: Avibe Sessions are long-lived (IM threads and Workbench Sessions that run for weeks), so an unbounded list
  would turn a working Session into a forced `/new`. `<earlier-record>` is left out when the adapter supplies no
  command, and `<current-request>` when the cut did not split a turn.
- `state`: texts the adapter rendered from their own stores when the checkpoint was written: the environment's core
  fields (C-7 §8: cwd, os, shell, date, timezone; no Watches, the cwd cut in the middle to 160 characters, so it is
  bounded by construction; a checkpoint always happens inside a run, and a Turn's own input carries only the fields
  that changed), then skill bodies by name (at most 5,000 tokens each) within the cap of the route the next request
  goes to: `state_cap = min(25,000, floor(0.1 * W))`, passed in `StateRequest.cap`. Nothing else is rehydrated in v1:
  pending work is the checkpoint's own "Waiting on", and the split turn's model-facing input, environment block
  included, is in `<current-request>`.
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
- (b) **Rolling**: fork-summarize the prefix up to the cut nearest half the tokens, moved earlier until its fork can
  fit (§5), so the context becomes checkpoint + the rest verbatim; roll again while the request still does not fit,
  at most 2 rolls per request.
- (c) **Dropped**: when no checkpoint request can help (one failed for this request other than by overflow, a rolling
  one failed, no rolling prefix can fit, or the rolls are used up), no model is called:
  the earliest part (§5) moves out of the context. Built like any checkpoint row (§7: fresh `state`, and
  `<current-request>` when the cut splits a turn), the row keeps the previous checkpoint's text and the
  `<earlier-record>` pointer; `checkpoint` is empty when there was none, and the message then has no framing text.
  Repeated while the request still does not fit.
- (d) **Stop**: when the request and its minimal request (what the drop would leave of it: the drop's own row, §8 c,
  plus the last unit; §3) both cannot fit (checked in the stage, §3,
  before provider admission), when nothing more can move out of a request that cannot fit or that the provider
  refused, after 4 provider overflows of one request, or while auto-compaction is paused (§10), the run ends
  `context_exhausted` and the `context_exhausted` event says what fills the context. No model is called for a
  context that cannot fit.

An attempt the provider refused, or that is retried, is never context (§10, invariant 4).

With a provider that always overflows, one request makes at most two checkpoint model calls, then drops
mechanically, and stops after its fourth overflow.

## 9. What the adapter supplies

The loop takes a `ContextConfig` (`harness/context.py`); without one, no context management runs and an overflow
ends the run `context_exhausted`, as in P1. The adapter supplies the following; the Avibe Agent's are in
`modules/agents/avibe` (`agent.py`, `context.py`, `store.py`), and every Turn of it runs with a `ContextConfig`.

- The limits (§1): the route's capabilities come from the Model Hub model definition the user edits
  (`context_window`, `max_output_tokens`; `input_limit` stays unknown until Model Hub stores one, so `L_in = W`), and
  the Agent asks for `min(max_output_tokens, floor(W / 4))` (the maximum 8,192 when unknown) on every hop it resolves,
  which is `O`.
- `scratch_dir`: this Session's scratch directory, `<state>/agent_core/scratch/<session_id>/`; without it, the
  policy denies every write.
- `ContextHost.earlier_record(session_id, through_seq)`: the lookup command, one `vibe data query` over the inputs
  and replies in `messages` of every Session whose rows the context holds (the Session and its fork ancestry, each
  up to its fork bound), through the last summarized `context_seq`, filtered by a `KEYWORD` the model replaces. It
  searches what the model read: the text of each row's model message (`content_json` at `$.model.message`, its
  text blocks decoded), else the display text. It reads `messages` only, which every caller of `vibe data query`
  may read; tool outputs can be run again.
- `ContextHost.render_state(StateRequest)`: the `state` texts (§7): the environment's core fields, then each skill
  the summarized rows loaded, by name, with the revision of the body it renders now (`<skill_content name revision>`,
  cut to 5,000 tokens with a note saying how to load all of it; a skill that no longer loads is named), within what
  `StateRequest.cap` leaves; room for the left-out notice is held back first, a skill is loaded only when there is
  room for it, and the skills left out are named (up to 20, then "and N more") with how to load them.
- Skill loads marked in their results: `vibe skill load` writes one `<skill_content name="...">` block per skill it
  loads, and every top-level block in a successful `bash` result is recorded in the result's `details.skills`
  (`[{name}]`), whatever the command looked like; blocks are parsed as balanced, so an example tag inside a skill's
  body is not a load. Clearing spares the result (§4) and a checkpoint carries the skills (§7).
- A `TranscriptStore` (C-5 §2) implementing the whole protocol C-9 uses: `append_response(..., request=...)` keeps
  `ModelResponse.request` for the anchor (§2); `append_payloads(session_id, entries)` writes several payload rows in
  one transaction (invariant 3); `append_audit(session_id, kind, payload)` writes a non-context audit row,
  `checkpoint_turn` (§6) or `attempt` (invariant 4), in every mode; every loaded row carries its `created_at` (§3).
  The SQLite store and the adapter's implement it, and one contract suite runs the same tests on them and on the
  in-memory store the engine tests use (`tests/test_transcript_store_contract.py`).
- The full environment block (C-7 §8) on the first input after a checkpoint: the summarized inputs that carried it
  are gone. The Avibe Agent works out an
  input's environment delta against the inputs the projected context keeps, so the input carries every field the
  context no longer shows, and forgets what it sent when a checkpoint commits.
- The one user-visible text of context management, through `vibe/i18n`: when a run ends `context_exhausted`, the
  stop message says the conversation has grown too long to continue reliably and suggests starting a new session
  with `/new`. Compaction itself, the pause included, shows nothing. A failure is recorded against the Model Hub route
  only when the served source produced it, which the loop states on every `error` event (`origin`, set where the
  error is raised): `source` for a C-2 provider error other than an overflow (our request was too large) or the
  loop's own abort, an answer the loop rejected (a failed or empty answer, a refusal, tool calls under `length` past
  the retries or under another stop), or a reply the transcript cannot hold; `local` for everything else (an
  overflow or a context that cannot fit, a Stop, a hook, tool, or store error). The adapter takes the failure (its
  kind, text, and attribution) from `run_ended.cause`, the error that decided the run's outcome, never from a
  diagnostic.

## 10. Guards

- **One compaction in flight per Session.** A checkpoint turn never starts another; it runs inside a run, and an
  Agent refuses a second run while one is active; across Agent instances the adapter's Session writer lock
  serializes them.
- **Pause.** Each failed checkpoint counts as a failure; a successful one resets the count. A successful
  normal checkpoint whose result is still at least `0.75 * T` counts as ineffective; an effective one resets that
  count. After 3 consecutive failures or 3 ineffective checkpoints, auto-compaction pauses for the Session: nothing
  is compacted (no checkpoint, no mechanical drop), a request that can fit is still sent, and one that cannot fit or
  that the provider refuses as overflow ends the run `context_exhausted` (§8 d). The `compaction_paused` event fires
  once, at the transition; the adapter shows nothing for it. The pause clears by itself after 30 minutes
  (`PAUSE_SECONDS`), or as soon as a request goes to another route (provider, api, model) than the one it began on,
  whichever comes first; both counters clear with it, and the next threshold tries again. The pause is decided first
  on every request, and while it holds nothing is built for a compaction (no hypothetical drop, no host call); the
  overflow ladder is one loop that reads the live guard before every step, so a checkpoint failure that begins the
  pause ends the ladder there (the request is sent if it can fit and was not refused), never in a drop. A
  guard transition belongs to the route its checkpoint request ran on, a retry's fallback included. The counters
  and the pause (`paused_at`, `paused_route`) are durable loop state, stored beside the hook state in `agent_state`
  rows (`AgentState.context`), so they survive restarts and forks; projection takes them from the latest
  `agent_state` row that carries them. A pause that clears commits at once, like every guard transition (invariant 3).

**Ordering and ownership invariants.** This list is the one normative statement of these rules; the other sections
refer to it. Each has a test in `tests/agent_core/agent/test_compaction.py` (route: `test_context.py`; the ledger's
every-exit audit: `test_loop.py`) that fails when its order or owner is broken.

1. **One request pipeline.** Projection, then `budget()` on that final request, once per composed request, then
   the C-9 stage, then the provider. Nothing changes a request after it is budgeted; a stage step that changes the
   context rebuilds the request from the top. Whether a request fits is always `budget()` on a request actually
   composed: the request to send, the fork the stage would send first (the checkpoint turn composes its requests the
   same way, so the dry run and the turn cannot differ), and the stop check's minimal request (§3: the drop's own
   row for that cut, built by the one builder of checkpoint rows and projected like any context; with fewer than two
   units, the request itself). The one bound
   that extends a budget by arithmetic is the checkpoint turn's tool room, from its latest request's budget (§6);
   the stop message after the fourth overflow re-measures the refused request. The single-unit stop (§8 d) is in
   the stage, before provider admission. Checkpoint requests take the same pipeline. In v1 an Agent with a
   `ContextConfig` takes no user hooks (a configuration error), so nothing else can rewrite a request C-9 owns;
   hooks with context management are a post-v1 design item (plan §10).
2. **One tool pipeline** in a checkpoint turn: the turn's budget (once closed, `BUDGET_USED`), then the checkpoint
   table (a denial is its fixed text), then execution (a scratch `write` or `edit` relative to the root's
   descriptor, as joined work an abort waits for), then the bound on the result of every call that ran (§6). The
   two fixed texts are never cut.
3. **One commit for C-9 state.** A transition (its `context_edit` rows, its `context_compaction` row, the guard and
   pause, and the hook state of that commit point in `AgentState`) is written in one transaction
   (`append_payloads`), before any event announces it; a failed commit leaves nothing. Every guard transition goes
   through it, the ones with no other row included (a failed checkpoint, a pause that clears). Nothing else writes
   `AgentState.context`.
4. **One attempt ledger.** Every model attempt of a run (success, overflow, error, retry, checkpoint) records its
   request, its response or partial, and so its usage, in one place. The checkpoint audit and the request facts of a
   committed response (the anchor) read only from it. An attempt that does not become the run's response is never
   context, and its partial is not carried: a retried request goes out again unchanged, and one the provider refused
   as overflow is rebuilt after a ladder step (§8). The ledger, not each exit path, keeps the usage of a
   conversation attempt that did not become a response row (retried, relieved, a usage-only terminal, refused at
   admission, or a failed commit): one non-context `ModelAttempt` audit row, never a response row, written in every
   mode, with or without `ContextConfig`, on every exit of the model call but an abort; a failed audit write is a
   diagnostic and is not retried. The cancelled run scope admits no further store write, so an aborted call's
   unaudited attempts are not kept. It is the one place that usage is kept. A checkpoint attempt's partials and
   usage are kept in its `CheckpointTurn` row (§6), which the turn writes by the same rule: once, on every exit but
   an abort, whichever step ends it (compose, provider, admission, the host's state or lookup, building the row,
   commit). A failed request or a host failure after the model answered is a failed checkpoint, counted by the
   guard; an engine error building the row, or a failed commit, ends the run with nothing landed (invariant 3).
5. **Route-scoped anchors.** An anchor answered by another origin (provider, api, model) is invalid (§2).
6. **One admission path.** Every response, of every purpose (conversation, checkpoint), and every partial with
   content is admitted in one place, the model call, before anything acts on it: it must be valid after the
   committed rows and, in a checkpoint turn, the turn's own request and messages (projection's rules: canonical
   JSON arguments, no duplicate open call ids). A conversation response that fails ends the run as a provider
   protocol violation; a checkpoint response that fails is a failed checkpoint, and none of its calls runs. Its
   audit keeps the response when JSON can hold it.

UX is silent: compaction is invisible, the user never needs to think about it, and the raw messages stay in
history. The only user-visible text is the stop message after (d) (§9).

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
</context-checkpoint-request>
```

## 12. Deferred

Not built in v1: background precomputed compaction; server-side compaction (forbidden by hard constraint 8);
automatic re-read of recently modified files; memory tools (the policy reserves their row); lowering effort for the
checkpoint turn through a per-message system effort.

Sources: the cut rule, cumulative file lists, and the overflow patterns from Pi (MIT, `7fbbd5f`); the merge rule from
OpenCode; "a record, not instructions" from Gemini CLI; fork delivery from Claude Code; the waiting-on section, the
earlier-record pointer, and the rolling ladder are Avibe's (research: evaluation §4).
