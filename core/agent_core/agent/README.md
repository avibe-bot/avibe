# Agent loop (P1)

The change contract is C-3 plus C-5 projection steps 1, 2, 5, and 6 from
`docs/plans/agent-core-contracts/`. Foundation types are unchanged.

## Integration

Import `Agent` from `agent.loop`, input/snapshot/hook types from `agent.hooks`,
and `ModelRouter`, `ModelSelection`, `ModelCapabilities` from `agent.models`.
The adapter supplies the Session id, working directory, explicit environment,
rebuilt system prompt, providers, transcript store, and job host.

```python
agent = Agent(
    session_id=session_id, models=router, tools=[], hooks=hooks,
    store=store, jobs=job_host, cwd=cwd, env=env, system=system,
)
# Job-backed tools receive this exact wrapper, not the unwrapped host.
agent.set_tools(build_tools(jobs=agent.jobs))
async for event in agent.run(AgentInput(message_id, rendered_input), run_id=turn_id):
    await deliver(event)
```

`steer` and `follow_up` are async and return whether the active run accepted the
input. `False` means the adapter keeps the persisted input in the P3 queue.
`abort`, `set_tools`, and `snapshot` are synchronous on the same event loop.
Only one run can use an Agent at a time. The adapter remains responsible for
excluding multiple Agent instances writing the same Session.

The router resolves endpoint and capabilities before each model attempt;
`provider_for(protocol)` selects transport after request hooks have run.
Before-model rewrites affect a detached request only. Tools execute against the
registry captured for that request, restricted to its advertised names.
A before-run setup affects that run only; `set_tools` changes the configured
set as well as the next request of an active run.

Hooks subclass `Hooks`, override async methods, and mutate `RunContext.state`
with JSON data. Rewrites compose in registration order; the first `Deny` or
`End` stops that hook chain. State changes append versioned `agent_state` rows
at commit points. A snapshot describes committed context and committed state.
The store/adapter owns fork ancestry; the engine does not copy parent rows.

## Invariants and evidence

- Responses are committed before `MessageCommitted`, and tool results before
  `ToolFinished`. Event sequences start at zero per Turn and increase strictly.
- Finality is decided with input admission locked before inserting the response.
  A final response closes admission; a competing late steer is refused.
- A tool batch runs sequentially and commits results in call order. Steers
  enter after the whole batch. Follow-ups enter at natural termination after
  steers have been consumed. A terminating tool still finishes its batch.
- Hook end commits the current step and records policy error results for
  unexecuted calls. It makes no further model request.
- Abort cancels the pending provider/tool/hook/backoff operation, waits for its
  cleanup, and kills tracked foreground jobs through `JobHost.kill`. Jobs
  handed to Watch survive. Closing the event iterator also cleans up its worker.
- Transient retries have a fixed attempt budget, exponential backoff, and honor
  Retry-After. Any streamed event or partial response forbids retry. Overflow
  emits an error and ends as `context_exhausted`.
- Projection consumes store-resolved ancestry, sorts by sequence, restores hook
  state, and settles orphan calls without executing commands or editing rows.
  The caller supplies rebuilt system/state messages and a read-only orphan
  settler. `JobOrphanSettler` reads JobHost status and caller-rendered governed
  results; Watch creation remains outside the pure projection.

`tests/agent_core/agent/` verifies C1, transient C2, C3, C4, C5, resume C6, and
C9 against the actual requests recorded by `tests/agent_core/fakes.py`.
It also covers each orphan job state, retry classification, cancellation during
job ownership transitions, and event consumer closure. These are in-memory
engine checks; production provider/store/Watch integration is a later layer.

## Known by design

- No compaction, context edits, overflow recovery, or provider transport here.
  A P3 row raises `ProjectionError`; it is never silently ignored.
- Store operations remain separate transactions under the queue lock (approved
  for v1 by the orchestrator). A crash between a non-final response and input
  consumption leaves that input queued for adapter recovery.
- Input rendering/environment deltas, delivery outbox, actual Session forking,
  job-to-call lookup, Watch creation, and output governance belong to the adapter
  or tools. The injected orphan callbacks return already governed content.
- A stream error may persist its partial response as non-final evidence, but no
  partial tool call executes. A later projection settles missing results.
- This lane validates engine boundaries with fakes. No live service, credentials,
  network provider, production state, or user-facing integration is exercised.
