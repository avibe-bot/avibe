# A reply is not the end of a Turn

Status: approved by the owner (cyhhao), 2026-10-19. PR 1: shared contract,
Vibey, and Claude's pre-steer phase typed as a reply. PR 2 and later: see Next
steps.

## Background

A Turn's reply was a `result` (green bubble, Web Push, unread, Inbox preview)
only when it was the Turn's terminal emit. The "Turn ended" signal rode on the
last reply (`MessageOutput.completes_turn`), so a reply's type encoded whether
it ended the Turn. When input was steered into a running Turn while the agent
was finishing a complete answer, that answer stopped being the end and was
demoted:

- the dispatcher stored a non-terminal `result` as `output`
  (`communication_type_for_output`);
- Vibey committed a reply a steer superseded as `output` (#2811);
- Claude emitted the pre-steer phase as `output` (`_emit_primary_phase_output`);
- Codex and OpenCode narrated it as `assistant`, shown on the Web as a muted
  `interim` copy only when long.

All of those render muted or not at all, and the steered input's answer became
the result. A silent final then needed the earlier text promoted back to
`result` (#2803, #2807, #2815). PR #2741 tried to avoid the demotion at
admission and was reverted (#2858); admission and steering stay as they are.

## Goal

Separate the two facts. What a reply is depends only on the reply. When the
Turn ends is a separate, content-free event from each backend's native
completion signal, and only that event does Turn-end work.

## Contract

1. Reply type depends only on the reply, never on whether it ends the Turn or
   who triggered it:
   - a complete reply (the model finished speaking for this step; no tool call
     follows in that step; not cut off at the output limit; non-empty after
     stripping `<silent>`) is a `result`: green, Web Push, unread. A Turn may
     have several `result` rows;
   - narration (text followed by tool calls in the same step) is `assistant`:
     Activity only, with the long-narration Web `interim` copy unchanged;
   - a silent reply shows nothing.
2. Per-backend detection of a complete reply:
   - Vibey: a response with no tool calls, not failed, not `length`-truncated,
     with display text;
   - Claude: the reply text of a completed result, including the phase
     completed before a steer was absorbed;
   - Codex: `agentMessage.phase` (`final_answer` and `partial_answer` are
     replies, `commentary` is narration); a null `phase` keeps today's rule (the
     turn's last visible message is the reply);
   - OpenCode: a completed assistant message whose finish is not `tool-calls`.
3. "The Turn ended" is content-free and driven by the backend's native signal
   (Vibey run end, Claude result with no pending steer, Codex `turn/completed`,
   OpenCode idle/settlement). It, and only it: hides the thinking indicator,
   closes the Activity group, finalizes IM (running reaction, status bubble,
   duration footer), settles the Run and sends its callback, writes the Turn's
   terminal snapshot, releases the runtime, and starts the next queued input.
   Failed and stopped Turns end with their outcome; error notices stay status
   pills.
4. Web Push: one push per `result` row.
5. Run settlement and callback text: the Turn's last visible reply, for every
   Run attached to the Turn, including one a steer attached after that reply.
   A Turn with no visible reply settles with empty text and sends no callback.
   The dispatcher, which writes the delivered Run copy, owns this through PR 2's
   typed Turn-end event; nothing reconstructs it afterwards. PR 1 keeps master's
   settlement: a Turn end settles with its own text, so a content-free end after
   a reply settles with empty text, as master does.
6. The demotion and promotion patches are deleted. Stored `output`, `interim`
   and promoted rows keep rendering as they do today.
7. Consumers that read `result` as the Turn's end move to the Turn-end signal
   or the durable Turn state.

## Shape of the change

### Dispatcher (`core/message_dispatcher.py`)

The terminal emit stays the Turn-end event: `emit_agent_message("result", …)`
with `output.completes_turn` is the only emit that releases the Turn, and an
empty body is its content-free form (already used by stops, failures and silent
finals). A backend whose reply coincides with its completion signal may still
carry the reply on that emit (one IM message with the done footer); the reply
part is delivered and typed exactly as a non-terminal reply would be.

- A non-terminal, non-detached `result` is persisted and delivered as `result`
  (`communication_type_for_output` no longer returns `output` for it).
- A Turn end settles its Runs and writes the Turn snapshot with its own text,
  as on master; a content-free end after a reply settles with empty text until
  PR 2 (rule 5).
- A Run's `result_payload.outputs` stays its append-only delivery log: every
  reply records its Run output when it is delivered, Claude's pre-steer phase
  included.
- A failed or stopped Turn reports its outcome, never an earlier reply, and a
  `level="silent"` end that carries text still records that text, as before.
- Claude: its completed pre-steer phase is now a non-terminal `result` that
  records its Run output.
- Codex and OpenCode: their terminal text already is the last visible reply
  (Codex keeps the last visible `agentMessage` as the result candidate and a
  silent message never displaces it; OpenCode substitutes the last visible
  completed reply for a silent final), so their Runs settle with it as today.
- `_stream_chunk` no longer derives Turn completion from `kind == "result"`.

### Message catalog (`vibe/message_types.json`)

`result.activityRole` becomes `reply`, a new role: it closes the current
Activity phase and is the agent's answer, but it does not end the Turn. Turn
end comes from the durable Turn row (`session_turns`), the IM `silent_terminal`
trace, `error`, or a backend-failure `notify`. No partial index reads
`activityRole`, so there is no migration.

### Vibey

- `_response_type`: a non-final reply a steer superseded commits as `result`.
- The adapter emits it as a non-terminal `result`; the run's final reply still
  rides the Turn-end emit at run end; a silent final is the content-free end.
- `committed_final` (restart recovery) no longer needs the row type to tell a
  superseded reply from a final: a superseded reply always has its steer after
  it, consumed (later `context_seq`) or accepted into the Turn and unconsumed.
  The legacy `output` check stays for rows released versions wrote.

## Consumer migration

| Consumer | Today | After |
| --- | --- | --- |
| `communication_type_for_output` | non-terminal `result` → `output` | stays `result` |
| silent-final promotion (`promote_turn_text_to_result`, `resumed_turn_text`) | retypes earlier `output`/`interim` to `result` | deleted |
| Claude pre-steer phase (`_emit_primary_phase_output`) | `output` | non-terminal `result` |
| Run `result_text` / callback on a silent final | empty | unchanged in PR 1; PR 2: the Turn's last delivered reply, settled by the dispatcher |
| Turn snapshot `result_text` on a silent final | empty | unchanged in PR 1; PR 2: as the Run |
| `_stream_chunk` default | `kind == "result"` completes the Turn | completion only when the caller says so |
| Activity grouping (`storage/agent_activity_service.py`) | `result` ends the Turn | `reply` closes the Activity group like a boundary; the durable Turn row / IM marker ends the Turn; promoted rows keep their recorded role; the Session's terminal Turns are read newest first through `ix_session_turns_session_created`, within the window and with no temporary sort |
| Live Activity card (`ChatPage`, `chatMessageTypes.ts`) | `result` settles the generation | `reply` resets it like a boundary; `turn.end` settles |
| Thinking bubble (`ChatPage`) | hides on a `result` row | hides on `turn.end` (or a failure terminal row) |
| Pet failure notice (`petState.latestFailureNotice`) | a `result` ends the lookup as a terminal | a reply ends the lookup |
| Fork anchors (`core/services/session_fork.py`) | a `result` after the anchor means the source completed | a reply or terminal after the anchor means completion only when the source has no running durable Turn |
| Vibey restart recovery (`committed_final`) | `output` means "not final" | finality from later or unconsumed input; `output` still read for released rows |
| Inbox awaiting-reply (`messages_service.list_inbox_sessions`) | durable Turn state of the latest input | unchanged: already Turn state; legacy fallback keeps `inboxSettlesReply` |
| `completes_turn` consumers (`base.emit_result_message`, `backend_failure`, `session_activities`, `scheduled_tasks`, Claude Activity outputs) | lifecycle authority | unchanged: it names the Turn-end emit |

## Deletions

PR 1: the dispatcher downgrade; the whole silent-final promotion
(`promote_turn_text_to_result`, `resumed_turn_text`, the dispatcher's legacy
`output` memo); Vibey superseded→`output`; Claude's pre-steer `output` type.

PR 2: Codex narration of the previous visible `agentMessage` and OpenCode's
superseded replies as `assistant`, with the rest of the backend migration.

## Compatibility

- Rows written by released versions keep their type: `output` renders muted,
  `interim` renders muted, a promoted `result` (`metadata.promoted_from`) keeps
  its recorded Activity role. `PROMOTED_FROM_KEY` stays readable; nothing
  writes it any more.
- `committed_final` still treats a stored `output` as not final.
- No schema or index change; `activityRole` is not part of any partial index.
- IM Turns have no durable Turn row; a visible IM reply closes its Activity
  group, and the IM `silent_terminal` trace is still written for a reply-less
  IM Turn end.

## Known by design

- PR 1 keeps master's silent-end Run settlement: a Turn that replied and then
  ended content-free (a silent final, a Vibey or Claude Turn whose steered
  input needed no answer) settles its Runs and its snapshot with empty text, as
  master does (master promoted only the Web row). Codex and OpenCode still
  substitute their last visible reply themselves. Settling with the Turn's last
  delivered reply is PR 2's (Next steps).
- On the Web the thinking indicator stays until `turn.end`, so it can show
  briefly under a final reply while the runner commits the Turn.
- In IM a reply that does not end the Turn is its own message without the done
  footer; the status bubble keeps running and is retired at the Turn end, as a
  phase output was.
- IM Turns have no durable running state, so a fork of an IM source taken
  mid-Turn reads a reply after its anchor as completion, as it read a `result`.

## Test plan

Scenario `MESSAGE-DELIVERY-334` (`tests/scenarios/message_delivery/catalog.yaml`).

- Dispatcher (owner boundary): a non-terminal reply followed by the steered
  input's reply both persist as `result`, each pushes once and is on the Run's
  output log once, and the Turn ends once after both, on its own final or on a
  content-free end that settles as master does; a silent final after a visible
  reply promotes nothing and pushes nothing new; narration stays `assistant`
  and leaves the Run text empty; a stop and a failure end the Turn once and
  carry no reply text; IM finalization (running indicator, status bubble)
  happens once, at the Turn end.
- Vibey (owner boundary): a complete reply followed by a steered input commits
  and delivers as `result` and pushes once, the steered reply is a second
  `result`, and the Turn ends once after both; a rerun after a restart promotes
  no stored `output`.
- Claude: a completed pre-steer phase emits a non-terminal `result` that
  records its Run output.
- Activity: the terminal-Turn read is pinned to an index-ordered SEARCH with no
  temporary sort.
- Activity grouping, fork completion, and the UI message-role helpers keep their
  rendering for single-reply Turns and treat a mid-Turn reply as a phase
  boundary.
- PR 2: one primary regression per backend at its owning boundary.

## Next steps

Approved by the owner, in this order:

1. PR 2. Claude, Codex and OpenCode move to the reply rule. In the same PR,
   every backend, Vibey included, reports output through one typed event set:
   reply, narration, tool activity, turn end (outcome), and error. This
   replaces type strings plus `MessageOutput` flags, so the dispatcher no
   longer infers semantics. Vibey's agent-event schema is the starting point.
   The same PR completes rule 5: the dispatcher, which writes the delivered Run
   copy, settles a Run whose Turn ends with no reply of its own with the Turn's
   last delivered reply, on the typed Turn-end event. Requirements found in
   PR 1's review: only a reply whose delivery succeeded counts; the text is the
   one the dispatcher wrote (quick replies and file links stripped, citations
   written out), not the backend's source text; it holds across a restart
   rerun and startup reconciliation, and for every Run attached to the Turn,
   one a steer attached after the reply included.
2. PR 3, structural only with no behavior change. One presentation decision
   point per output event: transcript or not, muted, push, IM delivery.
   Today's rules move into it unchanged as the default policy: the interim
   length threshold, hidden types, IM concise mode, `suppress_delivery`. The
   decision is recorded on the message.
3. PR 4, structural only with no behavior change. One admission entry for
   every input source: Web composer, IM, Harness, Show annotation, failure
   retry, agent-initiated. Each input is normalized to sender, kind and
   content, and its priority is decided through one default policy that holds
   today's per-source defaults. Who decided is recorded in the delivery
   history.

Out of scope for now: Mailbox, P2 (doorbell, owner-confirmed meaning, not
specified yet) and P4, decision models, firewall rules.
