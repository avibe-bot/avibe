# C-3 Loop control

The loop in `core/agent_core/agent/`. Semantics are the ones the bare-loop spike and Pi passed in the control matrix
(evaluation §1).

## 1. Surface

```python
class Agent:
    def __init__(self, *, models: ModelRouter, tools: Sequence[Tool], hooks: Sequence[Hooks],
                 store: TranscriptStore, jobs: JobHost) -> None: ...

    def run(self, input: Input, *, turn_id: str) -> AsyncIterator[AgentEvent]: ...   # one Avibe Turn
    def steer(self, message: Message) -> None: ...
    def follow_up(self, message: Message) -> None: ...
    def abort(self, reason: str) -> None: ...
    def set_tools(self, tools: Sequence[Tool]) -> None: ...
    def snapshot(self) -> Snapshot: ...
```

`Input` pairs a `UserMessage` with its `messages.id` row. `ModelRouter` returns the `ModelEndpoint` and
`ModelCapabilities` for the next call (C-2, C-6 via the adapter); `TranscriptStore` is C-5; `JobHost` is C-7. The loop
wraps the given `JobHost` (`Agent.jobs`) to track foreground jobs for `abort`; tools are constructed with that
wrapper.

## 2. One run

```text
commit input → loop:
    before_model hooks → provider stream (events out)
    if the response has tool calls:
        commit it as `assistant`
        for each call, in order: before_tool → execute → after_tool → commit tool result
        drain steers (commit each) → continue loop
    else, holding the queue lock:
        if a steer or follow-up is pending: commit the response as `assistant`, commit the pending inputs,
            release the lock → continue loop
        else: commit the response as `result` (final) and close the queues for this run, release the lock → end
```

Finality is decided before the response row is inserted, so a `result` row is always the run's last response. A
steer that arrives after the queues closed is refused by the running Turn, and Avibe's delivery falls back to the P3
queue, which starts the next run.

Tool calls in one response execute sequentially in call order in v1; results are always committed in call order.

## 3. Hooks

Hooks run in registration order. The first `deny` or `end` wins; rewrites compose in order.

| Hook | Receives | May return |
| --- | --- | --- |
| `before_run` | input message, run context | system prompt and tool set for this run |
| `before_model` | the `ModelRequest` about to be sent | a replacement request (transient rewrite of messages, system, tools, model, max tokens), or `end` |
| `after_model` | the committed assistant message | `skip_tools` (commit error results `[skipped by policy]` for its calls), or `end` |
| `before_tool` | the tool call | `deny(reason)` (error result with the reason), `alter_args(args)`, or `end` |
| `after_tool` | call and result | `alter_result(result)` before commit, or `end` |
| `after_run` | outcome | nothing |

A `before_model` rewrite changes only that request; permanent changes go through C-5 rows (`context_edit`,
`context_compaction`). `end` finishes the run after the current step commits.

## 4. Steer, follow-up, abort

- `steer(m)` queues `m`. It enters the context after the current tool batch and before the next model call, or, when
  the current response has no tool calls, it starts another model call in the same run. A steer never interrupts a
  running tool. Avibe P1 deliveries map to `steer`.
- `follow_up(m)` enters only when the run would otherwise end. It exists for hooks and the future tension system;
  Avibe's P3 queue starts a new run after the Turn settles instead.
- `abort(reason)` cancels the provider stream and the running tool: a foreground job's process tree is killed (Pi's
  behavior). Jobs already handed to Watch are not affected. The run ends with `run_ended{reason: "aborted"}`; what was
  committed stays committed.
- A tool result with `terminate: true` ends the run after the batch commits.

## 5. Tools per call

`set_tools` and a `before_model` rewrite take effect at the next model call. A call to a tool that is not in the set
sent with that request gets the error result `Tool <name> is not available.`

## 6. Agent events (C-4)

`run()` yields `agent-event.schema.json` events, keyed by `turn_id`. The adapter maps them onto existing Avibe concepts; no new UI nouns.

| Event | Avibe effect |
| --- | --- |
| `run_started` | none; the Turn was opened by its delivery |
| `text_delta`, `thinking_delta` | streaming progress (status bubble, Workbench live text) |
| `message_committed` | the committed `messages` row is delivered: `assistant` as activity, `result` as the reply |
| `tool_started` | `agent_events` `tool_call` trace row; IM progress line |
| `tool_progress` | status bubble update |
| `tool_finished` | the committed `tool_result` row; a handed-over job names its Watch |
| `steer_applied` | the steer delivery is accepted into the running Turn |
| `compaction_started`, `compaction_finished`, `compaction_failed` | optional status line; failures always reported |
| `run_ended` | `MessageOutput` settles the Turn |
| `error` | `notify` / `error` row |

## 7. Snapshot and fork

`snapshot()` returns `(session_id, context_seq, state)`, where `state` is the JSON object hooks keep. A hook that sets
state causes an `agent_state` row at the next commit point (C-5), so a fork starting at a later `context_seq` restores
it. Forking is Avibe's existing Session fork; the child's context resolves through C-5.
