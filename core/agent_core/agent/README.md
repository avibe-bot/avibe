# Agent loop (P1, C-9 in P3)

The change contract is C-3 plus C-5 projection from `docs/plans/agent-core-contracts/`,
and C-9 context management (`context.md`) when the Agent has a `ContextConfig`.
Foundation types are unchanged.

## Integration

Import `Agent` from `agent.loop`, input/snapshot/hook types from `agent.hooks`,
`ModelRouter` and `ModelSelection` from `agent.models`, and the shared
`ModelCapabilities` from `ai.provider`.
The adapter supplies the Session id, working directory, explicit environment,
rebuilt system prompt, providers, transcript store, and job host.

```python
agent = Agent(
    session_id=session_id, models=router, tools=[], hooks=hooks,
    store=store, jobs=job_host, cwd=cwd, env=env, system=system,
)
# Job-backed tools receive this exact wrapper, not the unwrapped host.
agent.set_tools(build_tools(jobs=agent.jobs))
async for event in agent.run(AgentInput(message_id, rendered_input), turn_id=turn_id):
    await deliver(event)
# Return accepted but unconsumed inputs after an early end, abort, or error.
pending = await agent.take_pending_inputs()
```

C-9 is on when the adapter passes `context=ContextConfig(host=..., scratch_dir=...)`
(`harness/context.py`). Before every model request the loop then clears old
tool results, writes a forked checkpoint at T, and walks the overflow ladder;
`agent.compact(turn_id=..., focus=...)` is `/compact`, refused while a run is
active. Without a config nothing changes and an overflow ends the run
`context_exhausted`. The `ContextHost` renders the `<earlier-record>` lookup and
the state a checkpoint carries; the store adds `append_checkpoint_turn` for the
checkpoint turn's audit row.

`steer` and `follow_up` are async and return whether the active run accepted the
input. `False` means the adapter keeps the persisted input in the P3 queue.
An accepted input remains owned by the Agent until consumed or returned by
`take_pending_inputs()`. Early termination preserves those queues, and a new run
is refused until they are collected. A returned input belongs to the Turn that
accepted it and is never re-queued as a new P3 Turn (`recovery.md` T3): after a
run that ended by design (a terminating tool or a hook `end`), the Avibe adapter
runs again for the returned inputs within the same Turn; after a stop or an
error, it admits them into the context, and they share the Turn's outcome.
`abort`, `set_tools`, and `snapshot` are synchronous on the same event loop.
Only one run can use an Agent at a time. The adapter remains responsible for
excluding multiple Agent instances writing the same Session.

The router resolves endpoint and capabilities before each model attempt;
`provider_for(protocol)` selects transport after request hooks have run.
An explicitly tool-incapable route is refused during preflight, before hooks or
input consumption. The run yields a clear error and terminal event without a
provider request. Unknown tool support still sends tools. Unknown/false image
and reasoning support is disabled; reasoning also requires a declared effort.
The configured output budget defaults to 8,192 and is capped by the provider's
maximum (8,192 when unknown); that cap is C-9's `O`. Every C-9 limit (`W` is
128,000 when unknown) comes from `harness.context.budget`, evaluated before each
model request on the route resolved for it.
The shared nullable source capabilities are not changed into guessed values.
Before-model rewrites affect a detached request only, including endpoint headers.
The router's cached selection is never mutated. Tools execute against the
registry captured for that request, restricted to its advertised names.
A before-run setup affects that run only; `set_tools` changes the configured
set as well as the next request of an active run.

Hooks subclass `Hooks`, override async methods, and mutate `RunContext.state`
with exact JSON data (string-keyed dicts, lists, JSON scalars, finite numbers).
Non-JSON Python values are hook errors, never silently coerced. Representation
comparison distinguishes booleans, integers and floats and avoids duplicate
state rows. Rewrites compose in registration order; the first `Deny` or
`End` stops that hook chain. State changes append versioned `agent_state` rows
at commit points. A snapshot describes committed context and committed state.
The store/adapter owns fork ancestry; the engine does not copy parent rows.

`AgentError.kind` is the adapter's stable error discriminator.
`AgentError.message` is engine/provider diagnostic detail, never display copy.
The adapter owns Session language, maps kinds to `vibe/i18n` messages, and tests
English/Chinese rendering at its delivery boundary. Model-facing tool result
text is separate from localized user-facing error delivery.
The stable `empty_response` kind means a tool-free final response had no
non-whitespace reply text (including thinking-only output); the adapter must
localize that error rather than deliver a silent success.

## Invariants and evidence

- Responses are committed before `MessageCommitted`, and tool results before
  `ToolFinished`. Hook state is written before the associated context entry, so
  a failed state write cannot hide a committed reply. A later input-write failure
  still announces prior committed rows. Event sequences start at zero per Turn
  and increase strictly; their identity field is `turn_id`.
- Finality is decided with input admission locked before inserting the response.
  A final response closes admission; a competing late steer is refused.
  A final refusal/safety stop retains the provider's text. Without a visible
  reply, it still commits a final result, then emits a typed error and ends.
  Other empty successful finals likewise stay committed, but emit
  `empty_response` and end as error. A received terminal is never retried.
  Empty non-final responses with queued inputs can still continue to a reply;
  tool calls, explicit hook end, and tool termination retain their contracts.
- A tool batch runs sequentially and commits results in call order. Steers
  enter after the whole batch. Follow-ups enter at natural termination after
  steers have been consumed. A terminating tool still finishes its batch.
  An `Exception` from `Tool.execute` becomes an error result (at most 500
  characters), with a logged traceback; the run continues. `BaseException`,
  including `CancelledError`, retains lifecycle semantics.
- Hook end commits the current step and records policy error results for
  unexecuted calls. `End` is a directive, not a primary outcome in the generic
  hook helper. The owning request/loop stage selects `ended_by_hook` only after
  its required state and step writes succeed; a failed required write ends in
  error, and abort around those writes retains the aborted outcome. Cleanup
  failures remain diagnostics. It makes no further model request.
- One explicit `RunScope` owns operation admission and joining on Python 3.10.
  Lazy factories prevent aborted runs from creating callback/store work.
  Abort cancels pending provider/tool/hook/backoff operations and waits for their
  cleanup. Admitted store commits finish before ownership is released; connected
  consumers still receive the committed-row event, and the run ends aborted.
  Cleanup runs in a separately joined task outside cancelled admission.
  Closing the event iterator also cleans up its worker.
  A dependency that independently raises cancellation produces an error and a
  terminal aborted event; it is not mistaken for a closed iterator.
- One outcome owner records the first primary cause. Stream closure, cleanup
  hooks and cleanup cancellation add diagnostics without overwriting that cause.
  The only exception is a foreground job that still cannot be killed, which
  upgrades completed to error. A received terminal is admitted and committed
  before closing its stream; close failure does not retry it or skip its tools.
  Every message-bearing write passes the projector's actual invariants through
  the pure `validate_message_append` helper before persistence. This covers
  initial/queued input consumption, full/partial responses, post-hook tool
  results (including terminating tools), and durable recovery results. Invalid
  candidates cannot poison resume: no unsupported message is appended/consumed,
  no committed-row event announces it, and prior valid entries remain intact.
  An invalid tool/hook result ends the run with an error; its call remains open
  for deterministic projection and T2 settlement. Recovery rejects invalid
  renderer output before append, so a corrected retry can finish settlement.
  The projector also rechecks every call's mutable arguments using the
  foundation's public `require_json_value`. Invalid JSON shapes raise
  `ProjectionError` with call and row context, before admission or replay;
  there is no separate loop-owned JSON argument validator.
- Every tool execution exit releases that `(Session, tool call)`'s remaining
  foreground handles, including success and failure. Final cleanup sweeps all
  remaining Session-owned handles on every terminal path. Handed-over jobs are
  never touched. Failed kills remain owned, other handles are still attempted,
  and an explicit cleanup error prevents a silently successful run.
- Transient retries have an attempt/time budget (at most 3 retries within 120
  seconds; default 2), exponential backoff, and honor Retry-After. An expired
  budget ends with the original error, including when delay or route resolution
  crosses the deadline. Route resolution precedes request admission: expiry is
  checked before route validation, projection, rehydration or hooks can run.
  Any streamed event or partial forbids retry. Without a `ContextConfig`, overflow
  emits an error and ends as `context_exhausted`; with one, it enters the C-9
  overflow ladder, which ends there only when nothing more can move out.
- Projection consumes store-resolved ancestry, sorts by sequence, restores hook
  state, and answers orphans with deterministic interrupted text. It has no job
  host or external settler. The caller supplies rebuilt system/state messages.
- Before resume, the adapter calls `agent.recovery.settle_open_calls` under its
  Session writer lock. It appends a governed result for each open call: exited
  output, a running job handed to Watch, or synthetic interrupted text.
  A call without job state (including `write` and `edit`) explicitly warns that
  it may or may not have completed and instructs the model to re-read the file
  before continuing. Each committed outcome is skipped on retry. Late settlement
  rows are projected alongside their original call; fork cuts only see included
  outcomes. After settlement the adapter marks the old Turn interrupted (T4);
  recovery never resumes the model or repeats the tool automatically.

`tests/agent_core/agent/` verifies C1, transient C2, C3, C4, C5, resume C6, and
C9 against the actual requests recorded by `tests/agent_core/fakes.py`.
It also covers each orphan job state, retry classification, cancellation during
job ownership transitions, settlement crash/retry windows, and event consumer
closure. One lifecycle table covers pre-start/storage abort, model/tool abort,
dependency cancellation, consumer closure, tool exceptions, normal completion,
and cleanup. Separate probes cover failed kills and cancellation during commit.
A primary-outcome/cleanup table checks reasons, durable rows, event order,
commit-before-close, tools after close failure, and cleanup diagnostics.
A hook-stage/required-commit table distinguishes successful end directives
from failed state/result writes and abort before, during, or after a commit.
The admission source matrix checks provider responses/partials, tools, result
rewrites, terminating tools, initial inputs and queued steer/follow-up inputs.
Recovery separately checks invalid renderer output after a prior good commit.
Both boundaries prove a fresh Agent can continue over the same store.
Valid-then-mutated argument cases cover Done and partial responses, with a
separate loaded-ancestry check. The final-reply table covers empty, whitespace,
thinking-only and visible replies across stop/length/refusal/safety and
pending-input continuation. The primary/cleanup table includes empty_response.
These are in-memory engine checks; production provider/store/Watch integration
is a later layer.

## Known by design

- No provider transport here. Checkpoint rows are projected from the text and
  state fixed when they were written; a malformed `context_compaction` or
  `context_edit` row raises `ProjectionError` and is never silently ignored.
- A checkpoint turn runs through `before_model` and `before_tool` only (the
  checkpoint policy last). Its responses and results are never committed, so
  `after_model` and `after_tool` do not see them, and it emits nothing but the
  compaction events.
- Store operations remain separate transactions under the queue lock (approved
  for v1 by the orchestrator). A crash between a non-final response and input
  consumption leaves that input queued for adapter recovery.
- Input rendering/environment deltas, delivery outbox, actual Session forking,
  job-to-call lookup, Watch implementation, and output governance belong to the
  adapter or tools. Recovery's renderer receives `(call, job_id, status, watch_id)`
  and returns a governed `ToolResult`; job keys include the original Session.
  The adapter serializes recovery with active runs. `JobHost.hand_over` must
  reuse a job's existing Watch when recovery retries an interrupted commit.
- A stream error may persist its partial response as non-final evidence, but no
  partial tool call executes. A later projection settles missing results.
- This lane validates engine boundaries with fakes. No live service, credentials,
  network provider, production state, or user-facing integration is exercised.
