# C-9 Context management

Frozen 2026-10-04 by owner decision; amended 2026-10-13 by owner decision (the estimate of opaque reasoning payloads
and the anchor's validity, §2; the cache lifetime per route, §3; the checkpoint request's output budget, §1 and §6);
amended 2026-10-15 by owner decision (the threshold stays below the route's context price step, §1).
The `harness` owns the rules (`core/agent_core/harness/context.py`, `projection.py`); the loop applies them before
every model request (`core/agent_core/agent/loop.py`); the adapter supplies the Session-specific parts (§9). Row
shapes: [`transcript-rows.schema.json`](transcript-rows.schema.json); events:
[`agent-event.schema.json`](agent-event.schema.json).

Three tiers, cheapest first:

1. **Output governance at write time** (C-7): no tool result exceeds 2,000 lines / 50 KB.
2. **Optional clearing of old tool results**, recorded as `context_edit` rows (§4). Off by default; an adapter may
   enable it for evaluation or rollout.
3. **Checkpoint**, recorded as a `context_compaction` row, written by the model in a forked request (§5–§7).

Compaction and clearing only append rows; they never rewrite or delete one. A fork anchored before a checkpoint
projects the full original context (C-5 §4).

## 1. Limits

The hop capabilities Model Hub returns for the route resolved for the request (C-6) give the limits. `O` is set
when the request is built, as its `max_tokens` (`output_tokens()`, `checkpoint_max_tokens()`). One pure function,
`harness/context.budget(request, capabilities, transcript, anchor)`, derives `W, L_in, M, P, T, keep` and the request's
`est` (§2) and reads `O` from the request, once per request (§10, invariant 1). No state keeps these values between
requests, so a route change is picked up at once; a smaller window than expected is handled by the overflow path
(§8). Every fit decision is `budget()` on a request actually composed (§10, invariant 1), including the two the
stage only considers: the fork it would send first, and the minimal request of the stop check.

```text
W    = context_window                              (128,000 when unknown)
L_in = input_limit                                 (W when unknown)
O    = the request's max_tokens: for a conversation request max_output_tokens (8,192 when unknown), capped by the
       Agent's output budget; for a checkpoint request min(64,000, that)
M    = min(max(8,000, ceil(0.03 * W)), floor(W / 8))
P    = the smallest context_price_steps size of at least 100,000 and below W   (none when there is none)
T    = min(L_in - O - M, floor(0.9 * W), floor(0.9 * P))     the compaction threshold; without P, the first two
keep = min(20,000, floor(0.25 * T))                the verbatim tail of a normal checkpoint
```

`O` is what the request asks for, not the most the route could produce. Model Hub model definitions are seeded from
models.dev, where many models list an output maximum as large as the window: in the snapshot checked on 2026-10-04,
1,337 of 8,150 entries with both limits have `max_output_tokens + M >= context_window` (so `T <= 0` and nothing could
be sent), and 296 more would leave `T < 0.25 * W`. The adapter therefore asks for
`min(max_output_tokens, floor(W / 4))` on every hop it resolves (a retry's fallback included), the maximum 8,192 when
unknown, so the output never takes more than a quarter of any window; the limits themselves still come from Model
Hub. `M` is at most an eighth of the window, so with `O <= W / 4` the threshold stays positive on every window:
`T >= 0.625 * W` (with `L_in = W`), and `T >= min(0.625 * W, 90,000)` with a price step `P` (below). Windows of
64,000 tokens and more are unchanged (`M = max(8,000, 3% W)`); on an 8,000-token route `O = 2,000`, `M = 1,000`, and
`T = 5,000`.

`P` is where the route's price steps up. `context_price_steps` (C-6) lists the input sizes past which the target
model's list price rises, from Model Hub's price table (the user's override file, then models.dev's first-party
`cost.tiers`). Past 272,000 input tokens, OpenAI's GPT-5.4 and later models bill the whole request at twice the input
and cache rates and 1.5 times the output, on a 1,050,000-token window; Gemini and Grok models step at 200,000. A long
request is slower as well: on the gpt-6-astra Sessions recorded from 2026-10-10 to 2026-10-15, with about the same
output, the median request took 23 s under 100,000 tokens, 43 s at 200,000 to 300,000, and 78 s past 800,000, and
65% of the requests were past the step (86% of their input tokens) at `T = 890,500`. Replaying those requests with
`T = 244,800` costs about a quarter at list price, checkpoints included, with 126 checkpoints instead of 9. So
`T` stays a tenth below the step, the window's ratio: the tenth leaves room for one tool batch and the checkpoint
request, so the checkpoint is written below the step too. A step under 100,000 tokens is not kept below: a
checkpoint's own context (system prompt, tools, checkpoint, `keep`) is some 30,000 tokens, so a lower threshold would
compact almost every request, and a dearer step above it still counts. A step at or past the window cannot lower
`T` and is ignored without arithmetic, whatever size a hand-edited price names. The step is the model's list price whatever
the Source bills (a subscription's request is as slow). With no price, or a flat one, `T` is as before.

A conversation request **fits** when `est + O + M <= L_in`, where `est` is its estimate (§2); `M` absorbs the
estimate's error. A request **can fit** when `est + O <= L_in`. A checkpoint request (§6) is sent only when it can
fit: at `est = T` it uses a few hundred tokens of `M` for the checkpoint request, and the provider remains the judge.

## 2. Token estimate

`tokens(x) = ceil(utf8_bytes(x) / 4)`: characters / 4 undercounts Chinese by about 65%, UTF-8 bytes / 4 is within 6%.
A message counts its text, its thinking text, each tool call's name and JSON arguments, and 1,600 tokens per image.

An opaque reasoning payload (a thinking or tool-call `signature`, redacted thinking: Anthropic's signed thinking, an
OpenAI Responses reasoning item's `encrypted_content`, and the like) is never counted like text. It is
ciphertext, about 17 UTF-8 bytes per reasoning token on OpenAI Responses, so bytes / 4 overstated a replayed item
about 4.3 times. A message carrying one counts, once, the larger of its signed thinking text and its reasoning: the
`reasoning_tokens` its usage reports, else its `output_tokens`, which include them; a response that reports no usage
counts its payloads' UTF-8 bytes / 6, the fewest bytes per reasoning token measured (Anthropic signatures 6.3-7.2,
OpenAI Responses items about 17), so it errs high. A failed or aborted partial's payloads count nothing: it is never
replayed (C-1). Measured on recorded Sessions: OpenAI Responses bills a replayed reasoning item at about its
`reasoning_tokens`; Anthropic bills replayed thinking at about its `reasoning_tokens` until a tool result before it
is edited, and not after that, which the next response's usage shows. A payload sent to another origin is dropped
(C-1); it still counts, until that route's first answer anchors.

The **anchor** is the latest response in the context that neither failed nor was aborted, reports non-zero usage,
and records the request it answered on its row (`ModelResponse.request.tokens`: `tokens()` of that whole request as
sent). It is rebuilt from the rows, so it holds across a new Agent and a new Turn.

```text
est    = usage(R) - tokens(R) + growth                     while the anchor R holds
growth = max(tokens(this request) - R.request.tokens, tokens(transcript) - tokens(R's transcript))
est    = max(est, tokens(this request))                    while a tool result before R differs (b)
       = tokens(this request)                              otherwise
usage(R) = input_tokens + cache_read_tokens + cache_write_tokens + output_tokens
```

`transcript` is what the rows put in this request (rehydrated messages aside); `R's transcript` is what they put
before `R` when its request was built. The anchor holds while (a) this request goes to the route that answered it
(`R.origin` equals the request endpoint's origin, because tokenizers differ), and (b) the transcript up to `R` is the
one `R`'s request carried, the content of tool results aside: the messages the rows put before `R` now are the ones
they put there then, but a tool result's content may differ (its call id, tool name, and error flag the same), and
`R` follows them. Clearing (§4) and a cut to fit (§3) change only that content, so the anchor survives them and the
growth takes away what they removed by its `tokens()`; what they removed is a local estimate taken from a provider
count, so while such a change stands the estimate is never below `tokens(this request)`. A checkpoint breaks (b), and
so does any other change before `R`. A changed system prompt, tool set, or rehydrated state does not invalidate the
anchor: the difference between this request's `tokens()` and `R.request.tokens` carries it, but only to add, since
the growth is never less than the transcript's own. So a recorded size larger than what the transcript's growth
explains (a row recorded under another estimate rule, such as one that counted payload ciphertext) cannot take the
estimate below `usage(R)` and what follows `R`. No rule version is recorded, so such a size hides growth of the
system prompt, tools, or rehydrated state up to its excess, until the next response anchors. Anything after `R`, an
edit there included, counts by its `tokens()`.

## 3. Before every model request

Every conversation request takes the request pipeline (§10, invariant 1). Its C-9 stage, inside the tool loop and
before a retry alike:

1. **Fit the newest tool batch, or stop** (§8 d) when the request cannot fit and neither can its minimal request:
   the request the drop (§8 c) would leave with everything but the last unit moved out (the drop's own row with its
   `state`, without the previous checkpoint's text, and the in-flight Turn's kept inputs, §5), budgeted without the
   anchor (its history would be gone). With fewer than two units nothing can move out, and the minimal request is
   the request itself. Moving the conversation out cannot help then, so when the last unit is a tool batch it is cut
   to fit: the minimal request with the margin `M` when it can, else without; the largest results are cut to one
   common cap (water-filling), each keeping its head (its text blocks joined; an image the text replacement cannot
   carry is dropped) and always ending with `[Output truncated to fit the context window; re-run with offset/limit
   or a narrower command to see more.]`, as one `context_edit` row per cut result
   (`"reason": "fit_tool_result"`; the rows keep the whole output, §4). This is the bound a checkpoint turn applies
   to its own tool results (§6), one helper for both. The run continues and compacts as usual. A batch that does not
   fit even with every result cut to its note, or a last unit that is an input, stops the run: the provider never
   sees the request. A batch that fits once the conversation moves out is never cut; the ladder makes room for it.
2. **Clear**, when enabled (§4), if the provider cache is cold (the request's route has not used it for longer than
   its lifetime, below; after a restart, measured from the response rows' `created_at`) or `est >= 0.8 * T`, which
   does not depend on the lifetime.
3. **Checkpoint** when `est >= T`, the run has not stopped compacting (§10), and there is something to summarize,
   at most once per model request: a normal checkpoint (§6, reason `threshold`) when the fork it would send can fit,
   otherwise the overflow ladder from (b) (§8, reason `overflow`). A checkpoint request that overflows continues at
   (b).
4. **Ladder** (§8) when the request does not fit. With no checkpoint request left to try (one failed for this
   request other than by overflow, or the run has stopped compacting), a request that can fit is sent and the
   provider judges. Once the run has stopped compacting nothing is compacted at all: a request that cannot fit, or
   that the provider refuses as overflow, ends the run `context_exhausted` (§10).

The cache lifetime is the documented lifetime of the cache mode Vibey requests for the conversation, where clearable
results live, resolved on every request from the route's provider, protocol, and whole model id
(`harness/context.py`, `CACHE_TTLS`):

| Route | Lifetime |
| --- | --- |
| provider `anthropic` on the Anthropic protocol: the message breakpoints' `cache_control` is ephemeral, with no `ttl` | 300 s, refreshed on use |
| provider `openai`, a model id `gpt-<major>[.<minor>][-<suffix>]` from GPT-5.6: `prompt_cache_options.ttl` is 30 minutes, its default and only value, and Vibey sends none | 1,800 s |
| any other route, or one not identified unambiguously (older OpenAI models, other vendors on either protocol, relays) | 3,600 s |

Clearing a warm cache forces a full-price re-read of the context, while skipping a clear on a cold one only keeps
some old output, so an unknown lifetime counts as an hour, and relays and subscription sources that keep a shorter
one err toward warm. The idle time is the route's own cache's: it runs from the later of the latest model call's end
and the latest response's commit served by the same provider and model as the request's route (the protocol that
reaches the gateway, converted or not, does not change the cache), which overstates warmth by a response's duration,
the same safe direction; another route's use never refreshes it. A route with no use in the Session yet takes the
latest use of any route.

A request the provider rejects as overflow (`ProviderError.kind == "overflow"`, classified by
`ai/errors.is_overflow_message`, HTTP 413, and `context_length_exceeded`) before anything was streamed, and with no
content in its partial, enters the overflow ladder (§8) and is rebuilt and sent again. Any other overflow is never
retried, even when the adapter reports no partial, since the user may have seen what was streamed: a partial with
content is committed as a non-final response, and the run ends `context_exhausted`. A checkpoint turn's own
requests (§6) never run this stage (no clearing, checkpoint, or ladder; one compaction is in flight per Session);
each is sent only when it can fit.

## 4. Optional clearing of old tool results

This policy is disabled by default. When disabled, no new `clear_old_tool_result` rows are written; the normal
`fit_tool_result` edits for a newest batch that cannot fit, checkpoints, and overflow ladder remain active. Existing
`clear_old_tool_result` rows continue to project as recorded, so disabling the policy never changes already persisted
Sessions.

- Eligible when enabled: results of `read` and `bash` in the projected context (terminal screen snapshots join when
  they exist).
- Protected: results after the second-latest input (the last 2 user turns; with fewer than 2 inputs since the latest
  checkpoint, all of them), the newest 5 eligible results, and results already cleared (a result cut to fit, §3, can
  still be cleared). A skill load clears like any
  other result: the model loads the skill again by name when it needs it, as after a checkpoint (§7). Results before the latest checkpoint are not in the context.
- Applied only when the candidates free at least 20,000 tokens, all at once, one `context_edit` row per result:
  `{"target_event_id": <tool_result row id>, "replacement": {"text": PLACEHOLDER}, "reason": "clear_old_tool_result"}`.
- `PLACEHOLDER` = `[Old tool result cleared to save context. Re-run the tool or re-read the file if you need it again.]`
- Projection keeps the call id, name, and error flag, and replaces the content with the placeholder. The latest
  edit of a target wins. The estimate's anchor survives it (§2).

## 5. Cut point

The projected context after the latest checkpoint is a list of **units**: an input, or a response together with the
results of its tool calls. A cut falls only before a unit: before a user message, or before an assistant message
whose tool batch follows it; never at a tool result, so no call is separated from its result.

- **Normal** (threshold, and the first overflow step): the tail is the longest run of whole units at the end
  whose tokens total at most `keep`, and at least the last unit; the in-flight Turn's inputs a cut keeps (below)
  count toward `keep` once, at their own size. Everything before it is the head. A head with nothing to summarize
  but those inputs means there is nothing to summarize.
- **Rolling** (§8 b): the cut nearest to half the tokens, moved earlier until the forked request over the head can fit
  (§1); never past the last unit.
- **Dropped** (§8 c): the cut nearest to half the tokens; never past the last unit.

Neither moves out nothing but the in-flight Turn's kept inputs.

`first_kept_seq` is the `context_seq` of the first kept unit. A cut inside the in-flight Turn keeps every input that
Turn consumed before it, its first and each accepted steer, verbatim and in order: the loop knows them (it consumed
them in this run; the rows carry no Turn identity, and a Turn's start cannot be told from their shape), and the row
records them (`kept_inputs`, their `context_seq`). Projection places them as they were, images, attachments, and
environment blocks included, right before the checkpoint message and the kept rows (§7), so the checkpoint's
`state`, the current environment, comes after any environment block they carry and the latest wins by position.
The checkpoint summarizes everything else in the head, and a later checkpoint inside the same Turn keeps them again.
In a later run they are ordinary history, yet no cut falls between them: one there would make an old input's seq the
new `first_kept_seq` and bring back what that checkpoint summarized, so cuts start at the first unit after them.
An earlier Turn a cut splits is summarized like any head. If the kept inputs alone cannot fit, nothing can move them
out, and §8 (d) applies.

## 6. Checkpoint turn

A checkpoint is written by a **side turn** of the conversation (C-10 `fork.md` §7), the only delivery path:

- The request uses the same hop, system prompt, tool definitions, tool choice, and reasoning settings as the
  conversation's next request, and its messages begin with the conversation's projected messages unchanged, so its
  prefix is byte-identical to the previous request's and a warm provider cache hits. Tool choice, thinking, and
  effort are never changed: Anthropic renders them into the prompt. `max_tokens = min(64,000, O)`. It bounds the
  reasoning as well as the checkpoint text, and a length stop fails the turn (below). Three checkpoints measured at
  `xhigh` effort on 246,000 to 307,000-token contexts wrote 9,200 to 10,600 tokens of checkpoint text, and 11,552 to
  20,644 tokens of output with their reasoning. Reasoning varied from 2,070 to 10,018 tokens and may grow with the
  context; a checkpoint near `T` on a 1,000,000-token window reads about three times those contexts. Output is billed
  as produced, so an unused budget costs nothing, and `T` already reserves `O` (§1), so the request can fit at
  `est = T`.
- **Normal**: the prefix is the whole projected conversation. **Rolling**: the prefix is the projected context up to
  the cut, which is a prefix of the original request.
- One user message is appended: the checkpoint request (§11). It takes the user role, but it comes from the harness,
  not the user, and its text says so.
- The turn's requests take the request pipeline (§10, invariant 1; a request that cannot fit is never sent and
  fails the attempt as an overflow), its responses the admission (invariant 6), and its tool calls the side turn's
  tool pipeline (invariant 2, C-10 §7 step 3). Its responses and results are never committed to the context, and
  nothing is shown to the user.

**Tool policy** ("dreaming": cognition allowed, actuation blocked), the side-turn policy `DREAMING` (C-10 §6), a
declarative table judged after the turn's budget (invariant 2). C-9 owns its values: the table and the two texts
below, and the budget's 5 rounds, 4,000-token floor, and 1,000-token slack; C-10 §7 step 3 applies them:

| Tool | Rule |
| --- | --- |
| `read` | allowed |
| `write`, `edit` | allowed only for a single file name directly inside this Session's scratch root, `<state>/agent_core/scratch/<session_id>/` (the path's directory is the root; a name with a separator, `.`, or `..` is denied). Scratch is flat and is reached only through a directory descriptor: when the turn starts, it creates the root and opens it once (`O_DIRECTORY \| O_NOFOLLOW`, `fstat` a real directory; a symlink authorizes nothing). Every read, temporary file (`O_CREAT \| O_EXCL \| O_NOFOLLOW`), and publication (`os.replace` with `src_dir_fd`/`dst_dir_fd`) is relative to that descriptor, so renaming or swapping the root, or a name in it, after it was opened redirects nothing; a name is never followed, and one that is not a regular file when the call runs is refused. Where the platform cannot open and rename relative to a descriptor (Windows), scratch writes are denied and `read` stays allowed. Each scratch call runs as joined work an abort waits for, so the descriptor closes only after its worker thread is done |
| memory-read tools | reserved: allowed once they exist |
| `bash` and every other tool | denied, never executed |

- A denied call gets the error result `This is a checkpoint turn: tools that act outside your own scratch space are
  unavailable. Write the checkpoint as your reply text, not into a file.` (a model denied a file write may otherwise
  keep trying one; the owner-approved request, §11, stays untouched).
- Every request of the turn is budgeted on the route resolved for it (§1): its `max_tokens` is that route's
  `min(64,000, O)`.
- The bound is the window, not `T`, the same way in threshold and rolling turns. Before each call,
  `room = L_in - est - min(64,000, O) - reserved`, where `est` is the budget of the turn's latest request plus the
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
  `This is a checkpoint turn and its tool budget is used up. Write the checkpoint as your reply text, not into a
  file.` before the table is
  consulted, so the audit's `rounds` can count one more response, the one answered that way. A response that still
  calls tools after that ends the turn as failed.
- `write` and `edit` in a checkpoint turn keep the tools' arguments and success texts (C-7); their errors are their
  own, short and without the C-7 advice to use `bash`, which the turn may not call. `edit` reuses the tools'
  matching (`edit_diff`), BOM, and line-ending handling. The C-7 tools themselves are unchanged.
- The checkpoint comes only from the turn's **final response**: the one that stops with `stop` and calls no tool
  (only a `tool_use` stop with calls continues the turn). Its text blocks are read; thinking is never read, and text
  in an earlier, tool-calling response of the turn is never used, a checkpoint block written there included. The
  validator, the same for every checkpoint turn (normal and rolling), judges what the user would see of that text:
  the product's one silent-block grammar (`core/reply_enhancer.strip_silent_blocks`, which delivery uses) removes its
  `<silent>` notes first. Then:
  - **Exactly one block.** One `<checkpoint>` tag, then one `</checkpoint>` tag (any letter case, blanks allowed
    inside the brackets, no attributes), and neither tag anywhere else in the visible text. A missing closing tag, a
    second block, a nested or stray tag, or a tag with attributes is no block. So the content cannot close the block
    early or open another one; a checkpoint that has to mention the tags describes them in words (§11).
  - **The block's text is the checkpoint.** The text between the tags, trimmed, is what the row keeps; text outside
    the block (narration before or after it) is ignored.
  - **A substance floor.** The checkpoint holds at least `min(128, max(32, W / 2,000))` tokens by the §2 estimate,
    where `W` is the window of the route the checkpoint was planned on. The floor only separates a checkpoint from a
    token reply (an acknowledgement, a pointer to a file, a bare "(none)"), which no checkpoint of a conversation
    long enough to compact is; it is not a quality bar. Up to a 64,000-token window it is 32 tokens, 128 UTF-8
    bytes, about two terse sentences (25 English words or 40 Chinese characters), so a short checkpoint on a small
    window passes. It grows with the window, whose compactions summarize more: on a 200,000-token window it is 100
    tokens, more than the template's headings with "(none)" under each (84), and from 256,000 tokens up it is 128.
    The estimate counts bytes, so the floor reads the same in every language.

  The template's three sections (§11) are writing guidance and are not judged: headings at any level, in any
  language, or none are accepted.
- **One repair.** A final response the validator rejects gets exactly one corrective follow-up in the same side turn
  (C-10 §7 step 4): the user message in §11 naming the problem (no block, a block that is not closed, tags that do not
  form exactly one block, an empty block, or a checkpoint under the floor). Its request takes the turn's pipeline,
  budget, policy, and admission like every other request of the turn, and its final response is validated the same
  way; a second rejection fails the turn.
- Anything else (a response refused at admission, a length stop, a `tool_use` stop without calls, calls under any
  other stop, an error, or a reply still rejected after the repair) is a failure: it counts as unproductive (§10),
  nothing enters the context, the old context is kept, and the ladder continues; the replies' text is only in the
  audit row. So is a checkpoint whose row the host cannot complete (§10, invariant 4).
- Framing: the checkpoint is shown later inside the summary's frame (§7), where the `<` of the frame's own tags in it
  is escaped; the block's tags never reach it (above).
- The turn's messages, read from the attempt ledger (§10, invariant 4) with every failed or retried attempt's
  partial and its usage, and the turn's tool results, are recorded once per checkpoint turn, on every exit but an
  abort (§10, invariant 4), outcome included, as the side turn's audit row (`agent_events.event_type = 'fork_turn'`,
  `visibility = 'audit'`, no `context_seq`; shape `ForkTurn` with `purpose: checkpoint` and `detail: {reason,
  mode}`; C-10 F7), never as context. A repair's follow-up is one of those messages, between the two replies, and the
  row's `repair` names the problem it named.

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
  - <path, cumulative across checkpoints, most recently touched first, the route's share of them>
  - and <N> more
  - and earlier ones (see the earlier record)
  Modified:
  - <path, cumulative across checkpoints, most recently touched first, the route's share of them>
  - and <N> more
  - and earlier ones (see the earlier record)
  </artifacts>
  <skills-loaded>
  Skills you had loaded are listed by name; run `vibe skill load <name>` again before you rely on one.
  - <name, most recently loaded first, at most 20>
  - and earlier ones (see the earlier record)
  </skills-loaded>
  <earlier-record>
  <the adapter's hint: where this Session's earlier conversation is stored, through summarized_to_seq>
  </earlier-record>
  The user message(s) placed just before this record are the current request and are still in progress; follow them exactly as written. Where this record disagrees with them, they win.
  </context-checkpoint>
  ```

  `Read` lists paths of successful `read` calls that no `write` or `edit` touched; `Modified` lists paths of
  successful `write` or `edit` calls; `(none)` when a list is empty. The row keeps each path as the call gave it, so a
  file's identity and whether it was read or modified are always decided on its original path; only what the model
  reads is cut. Each list keeps its 50 most recently touched paths in the row and marks, once and for good, that a
  path was pushed out (`files_read_omitted`, `files_modified_omitted`); the text shows the route's share of them,
  `clamp(floor(W / 4,000), 5, 50)` for the route the conversation's next request goes to (5 on 8K, 8 on 32K, 50 from
  200K), counts the stored rest in "and N more", and when the mark is set ends with "and earlier ones" (pointing to
  the earlier record when there is one). Only what is exact is counted: a path pushed out and touched again is the same
  path, so a count of pushed-out paths would grow with every cycle and say what is not. This is an Avibe deviation
  from Pi, which keeps every path: Avibe Sessions are long-lived (IM threads and Workbench Sessions that run for
  weeks), so an unbounded list would turn a working Session into a forced `/new`. `<skills-loaded>` lists the skills
  the summarized rows loaded (§4's marks, minus those whose load result the kept rows still hold), by name only: the
  most recently loaded first, at most 20, cumulative, stored as loaded, with the same mark (`skills_omitted`) and line
  for the ones pushed out; it is left out when there are none. A skill's instructions are never injected: the model loads the skill again, and `vibe skill
  load` checks the name against the catalog. Every path and name the model reads is one line of plain text
  (`display`): escaped (`escape`: a backslash doubled, then control characters and `<`, `>` as `\uXXXX`, or
  `\UXXXXXXXX` past the BMP), then cut in the middle to 160 UTF-8 bytes on a character boundary (its head and its file
  name stay), at most about 40 tokens whatever the script, so a filename can neither add a line nor close a tag.
  Escaping is injective, so two paths look alike only when a cut hides where they differ. `<earlier-record>` is the adapter's short hint, left out when it supplies none. The last line is harness framing, written only when the row keeps the in-flight Turn's inputs (`kept_inputs`, §5): a mid-turn checkpoint may misstate the Turn (as finished, or as more than was asked), and the verbatim inputs placed before it must win; the owner-approved request (§11) is unchanged.
- `state`: texts the adapter rendered from their own stores when the checkpoint was written: the environment's core
  fields (C-7 §8: cwd, os, shell, date, timezone; no Watches; each field displayed as every input's block displays it,
  the cwd escaped and never cut, every other field cut to 160 UTF-8 bytes (`display`), so the block is the cwd, which
  the OS bounds, plus at most about 4 x 40 tokens; a checkpoint always happens inside
  a run, and a Turn's own input carries only the fields that changed). Nothing else is rehydrated in v1: skills are
  listed by name in `<skills-loaded>`, pending work is the checkpoint's own "Waiting on", and the in-flight Turn's
  inputs stay as they were (§5).
- Projection (C-5 §3): the system prompt, hook-rehydrated messages, the kept inputs (§5), one user message
  holding `summary` and then each `state` text as its own text block, then the rows from `first_kept_seq` on, with
  edits applied. A tool result whose
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
  the earliest part (§5) moves out of the context. Built like any checkpoint row (§7: fresh `state`, and the
  in-flight Turn's inputs stay), the row keeps the previous checkpoint's text and the
  `<earlier-record>` pointer; `checkpoint` is empty when there was none, and the message then has no framing text.
  The previous checkpoint's text is kept only if the minimal request (§3) carrying it can fit: a checkpoint written
  on a larger route may not fit a smaller fallback. Otherwise the row has no model text, and its message says, in
  place of the framing, "An earlier checkpoint was too large for this model and was omitted; see the earlier
  record." (the pointer clause only when there is an `<earlier-record>`); the history stays retrievable there.
  Repeated while the request still does not fit.
- (d) **Stop**: when the request and its minimal request (what the drop would leave of it: the drop's own row, §8 c,
  plus the last unit; §3) both cannot fit (checked in the stage, §3,
  before provider admission), when nothing more can move out of a request that cannot fit or that the provider
  refused, after 4 provider overflows of one request, or once the run has stopped compacting (§10), the run ends
  `context_exhausted` and the `context_exhausted` event says what fills the context. No model is called for a
  context that cannot fit. The error's kind names what is too large, decided by measurement when moving the
  conversation out cannot help: a part of the newest unit is named only when the request the stop judged (the
  minimal request, or the request itself) would fit without it. `tool_output_too_large` when that part is the tool
  batch's results (even cut to their notes, §3), `step_too_large` when it is the step itself (its tool-call arguments
  or text), `input_too_large` when it is an input; otherwise `context_exhausted`, the conversation's length. A
  request the provider refused fit the estimate, so its excess is unknown: the results are named while the cuts left
  them above their notes, the step once they are at their notes.

An attempt the provider refused, or that is retried, is never context (§10, invariant 4).

With a provider that always overflows, one request makes at most two checkpoint model calls, then drops
mechanically, and stops after its fourth overflow.

## 9. What the adapter supplies

The loop takes a `ContextConfig` (`harness/context.py`); without one, no context management runs and an overflow
ends the run `context_exhausted`, as in P1. The adapter supplies the following; Vibey's are in
`modules/agents/vibey` (`agent.py`, `context.py`, `store.py`), and every Turn of it runs with a `ContextConfig`.

- The route's identity: its endpoint's `provider`, `protocol`, and `model_id` (the hop's `provider` and
  `runtime_model`), from which the loop resolves the cache lifetime on every request (§3); nothing configures it.
- The limits (§1): the route's capabilities come from the Model Hub model definition the user edits
  (`context_window`, `max_output_tokens`; `input_limit` stays unknown until Model Hub stores one, so `L_in = W`), and
  the Agent asks for `min(max_output_tokens, floor(W / 4))` (the maximum 8,192 when unknown) on every hop it resolves,
  which is `O`.
- `scratch_dir`: this Session's scratch directory, `<state>/agent_core/scratch/<session_id>/`; without it, the
  policy denies every write.
- `ContextHost.earlier_record(session_id, through_seq)`: a short factual hint: the earlier messages (inputs and
  replies, through `through_seq`) are in Avibe's `messages` table under `session_id`, and tool outputs are not, so
  the model re-runs a tool for its output (`messages` is readable by every caller; tool results are not). Then one
  runnable example, a fixed query with no CTE and no decoding: `vibe data query --sql "SELECT context_seq, type,
  substr(content_text,1,500) FROM messages WHERE session_id='<id>' AND context_seq <= <N> ORDER BY context_seq DESC
  LIMIT 20"`, emitted only when the id is a plain token (letters, digits, `_`, `-`), so the line always runs as
  written. For a fork, one more line names the Session it was forked from and at which `context_seq`.
- `ContextHost.render_state(StateRequest)`: the `state` texts (§7): the environment's core fields.
- Skill loads marked in their results: `vibe skill load` writes one `<skill_content name="...">` block per skill it
  loads, and every top-level block in a successful `bash` result is recorded in the result's `details.skills`
  (`[{name}]`), whatever the command looked like; blocks are parsed as balanced, so an example tag inside a skill's
  body is not a load. A checkpoint lists the names (§7); the result clears like any other (§4). A command that only
  prints such a block (a `cat` of a file) is marked too; that lists a name the model may reload, which `vibe skill
  load` checks, so it costs a line and injects nothing.
- A `TranscriptStore` (C-5 §2) implementing the whole protocol C-9 uses: `append_response(..., request=...)` keeps
  `ModelResponse.request` for the anchor (§2); `append_payloads(session_id, entries)` writes several payload rows in
  one transaction (invariant 3); `append_audit(session_id, kind, payload)` writes a non-context audit row,
  `fork_turn` (§6, C-10 §7) or `attempt` (invariant 4), in every mode; every loaded row carries its `created_at`
  (§3).
  The SQLite store and the adapter's implement it, and one contract suite runs the same tests on them and on the
  in-memory store the engine tests use (`tests/test_transcript_store_contract.py`).
- The full environment block (C-7 §8) on the first input after a checkpoint: the summarized inputs that carried it
  are gone. Vibey works out an
  input's environment delta against the inputs the projected context keeps, so the input carries every field the
  context no longer shows, and forgets what it sent when a checkpoint commits.
- The one user-visible text of context management, through `vibe/i18n`: when a run ends `context_exhausted`, the stop
  message says the conversation has grown too long to continue reliably and suggests starting a new session with
  `/new`. Compaction itself, a failed checkpoint included, shows nothing. A failure is recorded against the Model Hub
  route only when the served source produced it, which the loop states on every `error` event (`origin`, set where the
  error is raised): `source` for a C-2 provider error other than an overflow (our request was too large) or the loop's
  own abort, an answer the loop rejected (a failed or empty answer, a refusal, tool calls under `length` past the
  retries or under another stop), or a reply the transcript cannot hold; `local` for everything else (an overflow or a
  context that cannot fit, a Stop, a hook, tool, or store error). The adapter takes the failure (its kind, text, and
  attribution) from `run_ended.cause`, the error that decided the run's outcome, never from a diagnostic.

## 10. Guards

- **One compaction in flight per Session.** A checkpoint turn never starts another; it runs inside a run, and an
  Agent refuses a second run while one is active; across Agent instances the adapter's Session writer lock
  serializes them.
- **A per-run bound on unproductive checkpoints.** A checkpoint attempt that fails, or a normal one whose result is
  still at least `0.75 * T` (ineffective), is unproductive; an effective one is not counted. After 2 unproductive
  attempts in one run (`UNPRODUCTIVE_CHECKPOINTS`), the run stops compacting: nothing more is compacted in it (no
  checkpoint, no mechanical drop), a request that can fit is still sent, and one that cannot fit or that the
  provider refuses as overflow ends the run `context_exhausted` (§8 d). The bound is read live first on every
  request, so nothing is built for a compaction it forbids (no hypothetical drop, no host call), and before every
  step of the overflow ladder, which is one loop, so an attempt that uses it up ends the ladder there, never in a
  drop. It is held in memory and never persisted: the next run starts at zero, whatever route or restart lies
  between, so wasted checkpoint spend is at most 2 attempts per Turn. Nothing of it is shown to the user.

**Ordering and ownership invariants.** This list is the one normative statement of these rules; the other sections
refer to it. Each has a test in `tests/agent_core/agent/test_compaction.py` (route: `test_context.py`; the ledger's
every-exit audit: `test_loop.py`) that fails when its order or owner is broken.

1. **One request pipeline.** Projection, then `budget()` on that final request, once per composed request, then
   the C-9 stage, then the provider. Nothing changes a request after it is budgeted; a stage step that changes the
   context rebuilds the request from the top. Whether a request fits is always `budget()` on a request actually
   composed: the request to send, the side turn the stage would send first (`side_turn_fits`, which composes the
   caller's rehydrated messages and prefix exactly as the side turn does, so the dry run and the turn cannot differ;
   C-10 §7), and the stop check's minimal request (§3: the drop's own
   row for that cut, built by the one builder of checkpoint rows and projected like any context; with fewer than two
   units, the request itself). The one bound
   that extends a budget by arithmetic is the checkpoint turn's tool room, from its latest request's budget (§6);
   the stop message after the fourth overflow re-measures the refused request. The single-unit stop (§8 d) is in
   the stage, before provider admission. Checkpoint requests take the same pipeline. In v1 an Agent with a
   `ContextConfig` takes no user hooks (a configuration error), so nothing else can rewrite a request C-9 owns;
   hooks with context management are a post-v1 design item (plan §10).
2. **One tool pipeline** in a checkpoint turn, the side turn's (C-10 §7 step 3): the turn's budget (once closed,
   `BUDGET_USED`), then the checkpoint table (a denial is its fixed text), then execution (a scratch `write` or
   `edit` relative to the root's descriptor, as joined work an abort waits for), then the bound on the result of
   every call that ran (§6). The two fixed texts are never cut.
3. **One commit for C-9 state.** A transition (its `context_edit` rows or its `context_compaction` row, and the hook
   state of that commit point in `AgentState`) is written in one transaction (`append_payloads`), before any event
   announces it; a failed commit leaves nothing. C-9 keeps no other durable state.
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
   usage are kept in its side turn's `fork_turn` audit row (§6, C-10 F7), written by the same rule: once, on every
   exit but an abort, whichever step ends it (compose, provider, admission, the host's state or hint, building the
   row, commit). A failed request or a host failure after the model answered is a failed checkpoint, counted by the
   run's bound; an engine error building the row, or a failed commit, ends the run with nothing landed (invariant 3).
5. **Route- and transcript-scoped anchors.** An anchor answered by another origin (provider, api, model) is invalid,
   and so is one whose transcript a checkpoint, or any change but a tool result's content, altered; while a tool
   result before it differs, the estimate is at least the request's own `tokens()` (§2).
6. **One admission path.** Every response, of every purpose (conversation, checkpoint), and every partial with
   content is admitted in one place, the model call, before anything acts on it: it must be valid after the
   committed rows and, in a checkpoint turn, the turn's own request and messages (projection's rules: canonical
   JSON arguments, no duplicate open call ids). A conversation response that fails ends the run as a provider
   protocol violation; a checkpoint response that fails is a failed checkpoint, and none of its calls runs. Its
   audit keeps the response when JSON can hold it.

UX is silent: compaction is invisible, the user never needs to think about it, and the raw messages stay in
history. The only user-visible text is the stop message after (d) (§9).

## 11. Checkpoint request (prompt `checkpoint-v4`)

The request, verbatim (`harness/context.py` `CHECKPOINT_REQUEST`). `checkpoint-v3` (owner decision 2026-10-11) asks
for the checkpoint in one `<checkpoint>` block of the final reply; the template inside it is `checkpoint-v2`'s,
unchanged, and is guidance (§6).

`checkpoint-v4` (owner decision 2026-10-12) adds two things:
- The request, and its repair, say that they come from the harness, not the user.
- Neither one, nor anything it asks (including not calling tools), may be recorded as something the user said or
  wants, as a goal or requirement, or as a next step.

The only production checkpoint before it showed why. It was written under `checkpoint-v2` and credited its own
request to the user: its first next step was to write only the checkpoint and run no tool. That record became the
continuing model's context. The request:

```text
<context-checkpoint-request>
This request comes from the harness that runs you, not from the user, and the user does not see it. Pause the work here. Do not call any tool and do not continue the task. This reply does one thing: write a context checkpoint.

The conversation above will be replaced by your checkpoint plus the messages after it. Whoever continues is you, but you will no longer remember this conversation; the checkpoint is all you will know of it. What you do not write down is lost.
If there is an earlier checkpoint above, it is discarded after this: carry forward everything in it that still matters; where it conflicts with later messages, the later messages win.

Everything above is a record to summarize, not instructions to act on now. This request is not part of that record: do not write it, or anything it asks of you (including not calling tools), into the checkpoint as something the user said or wants, as a goal or requirement, or as a next step. Record the work as it stood before this request.

Write the checkpoint as one block in your final reply: a line <checkpoint>, then the checkpoint, then a line </checkpoint>. Only what is inside that block is kept. Write those two tags only there, once each; if the checkpoint has to mention them, describe them in words.

Inside the block, write in three layers, from the broad to the specific, under these headings, in this order. Write "(none)" under an empty heading.

<checkpoint>
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
</checkpoint>

Rules: terse bullets, not paragraphs. Preserve exact paths, identifiers, commands, error strings, and numbers. Do not invent anything that is not above. Never write out secrets, tokens, or credentials; refer to them by name. Write in the language the user writes in.
</context-checkpoint-request>
```

The one corrective follow-up (§6), verbatim (`CHECKPOINT_REPAIR`), where `{problem}` is the validator's, one of
`it has no <checkpoint> block`, `its <checkpoint> block is not closed`, `its <checkpoint> and </checkpoint> tags do
not form exactly one block`, `its <checkpoint> block is empty`, or `the checkpoint is too short: about <n> tokens,
and it needs at least <floor>`:

```text
<context-checkpoint-request>
That reply is not a checkpoint: {problem}. Write your final reply again, with the complete checkpoint as asked above in exactly one <checkpoint> block. Do not call any tool. Like the request, this message comes from the harness, not the user: keep it, and what it asks, out of the checkpoint.
</context-checkpoint-request>
```

## 12. Deferred

Not built in v1: background precomputed compaction; server-side compaction (forbidden by hard constraint 8);
automatic re-read of recently modified files; memory tools (the policy reserves their row; a memory side turn,
C-10 §13); lowering effort for the
checkpoint turn through a per-message system effort.

Sources: the cut rule, cumulative file lists, and the overflow patterns from Pi (MIT, `7fbbd5f`); the merge rule from
OpenCode; "a record, not instructions" from Gemini CLI; fork delivery from Claude Code; the waiting-on section, the
earlier-record pointer, and the rolling ladder are Avibe's (research: evaluation §4).
