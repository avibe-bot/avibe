# A reply is not the end of a Turn

Status: approved by the owner (cyhhao), 2026-10-19. PR 1: shared contract and
Vibey. PR 2: Claude, Codex, OpenCode.

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
5. Run settlement and callback text: the Turn's last visible reply. A Turn with
   no visible reply settles with empty text and sends no callback. Precisely
   (orchestrator decision, 2026-10-20): a Run settles with the latest reply
   recorded while it was attached. A Run steered in after the Turn's last
   reply, whose Turn then ends silently, settles empty and gets no callback,
   because that reply answered an earlier input, not this Run.
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
- One owner per consumer, both durable, no in-memory copy:
  - Run `result_text` and callback: each Run's own output ledger
    (`agent_runs.result_payload.outputs`), where every reply the Run was
    attached to is recorded when it is delivered (IM records before its row is
    written). A Turn end without its own visible text that completes the Turn
    (not failed, not a resultless settlement) settles each Run with the latest
    entry recorded on it for this Turn (`provenance.turn_id`), not detached,
    under that entry's own id and text, and appends nothing
    (`record_turn_run_outputs(settles_with_recorded_reply=True)`, inside the
    settlement write that already holds the Run rows). A Run with no such entry
    settles empty. The ledger keeps first-arrival order and never re-adds an
    id, so a retried earlier reply cannot displace a later one, and a restart
    reads the same entries.
  - Turn snapshot `terminal_evidence.result_text`: what the Turn end itself
    carried, so "" for a content-free end. Its only reader settles Runs
    accepted into the Turn too late for its end, which by the rule above have
    no recorded reply.
- For a reply, the ledger is the record: a Run settles with the reply recorded
  on it even when its transcript write failed, as a final reply's Run does.
  The transcript is the record for what the UI renders. Nothing is promoted or
  re-notified.
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
| silent-final promotion (`promote_turn_text_to_result`, `resumed_turn_text`) | retypes earlier `output`/`interim` to `result` | PR 1: never for narration or in a Turn with a reply; PR 2: deleted |
| Run `result_text` / callback on a silent final | empty | the latest reply recorded on that Run's ledger for this Turn, under its own id; empty for a Run attached after it |
| Turn snapshot `result_text` on a silent final | empty | unchanged: what the end carried ("") |
| `_stream_chunk` default | `kind == "result"` completes the Turn | completion only when the caller says so |
| Activity grouping (`storage/agent_activity_service.py`) | `result` ends the Turn | `reply` closes the Activity group like a boundary; the durable Turn row / IM marker ends the Turn; promoted rows keep their recorded role |
| Live Activity card (`ChatPage`, `chatMessageTypes.ts`) | `result` settles the generation | `reply` resets it like a boundary; `turn.end` settles |
| Thinking bubble (`ChatPage`) | hides on a `result` row | hides on `turn.end` (or a failure terminal row) |
| Pet failure notice (`petState.latestFailureNotice`) | a `result` ends the lookup as a terminal | a reply ends the lookup |
| Fork anchors (`core/services/session_fork.py`) | a `result` after the anchor means the source completed | a reply or terminal after the anchor means completion only when the source has no running durable Turn |
| Vibey restart recovery (`committed_final`) | `output` means "not final" | finality from later or unconsumed input; `output` still read for released rows |
| Inbox awaiting-reply (`messages_service.list_inbox_sessions`) | durable Turn state of the latest input | unchanged: already Turn state; legacy fallback keeps `inboxSettlesReply` |
| `completes_turn` consumers (`base.emit_result_message`, `backend_failure`, `session_activities`, `scheduled_tasks`, Claude Activity outputs) | lifecycle authority | unchanged: it names the Turn-end emit |

## Deletions

PR 1: the dispatcher downgrade, narration (`interim`) promotion, Vibey
superseded→`output`. The silent-final promotion stays only for a stored
`output` and only in a Turn that has no `result` reply, because Claude's
pre-steer phase still emits `output` until PR 2 (Codex and OpenCode already
choose their last visible reply themselves).

PR 2: Claude's pre-steer `output` branch, Codex narration of the previous
visible `agentMessage`, OpenCode's superseded replies as `assistant`, and the
rest of the promotion (`promote_turn_text_to_result`, `resumed_turn_text`, the
dispatcher's legacy `output` record).

## Transition from PR 1 to PR 2

PR 1 keeps one transitional patch, approved by the orchestrator on
2026-10-20 as contract-preserving: a silent final still promotes a stored
legacy `output` in a Turn that has no `result` reply. It exists only because
Claude's pre-steer phase emits `output` until PR 2. PR 2 must delete it in the
same change as Claude's pre-steer `output` branch:

- [ ] `core/message_dispatcher.py`: `_turn_visible_text`, `_remember_turn_text`,
  `_take_turn_text`, `remembers_turn_text`, and the promotion block of the
  content-free Turn end;
- [ ] `core/message_mirror.py`: `promote_turn_text_to_result`,
  `resumed_turn_text`;
- [ ] `tests/test_message_dispatcher_silent_final.py` and
  `MESSAGE-DELIVERY-325` (retired in favour of `MESSAGE-DELIVERY-334`);
- [ ] `storage/agent_transcript.py::last_turn_text`, whose only caller is
  `resumed_turn_text` (a forced `ix_messages_turn_id` read of the Turn's rows);
- [ ] the line that drops a Turn's legacy `output` record when a `result`
  reply is delivered (`replies_mid_turn` in `emit_agent_message`);
- [ ] keep reading `PROMOTED_FROM_KEY` / `PROMOTED_FROM_TYPES` for released
  promoted rows (`storage/agent_activity_service.py`).

## Compatibility

- Rows written by released versions keep their type: `output` renders muted,
  `interim` renders muted, a promoted `result` (`metadata.promoted_from`) keeps
  its recorded Activity role. `PROMOTED_FROM_KEY` stays readable.
- `committed_final` still treats a stored `output` as not final.
- No schema or index change; `activityRole` is not part of any partial index.
- IM Turns have no durable Turn row; a visible IM reply closes its Activity
  group, and the IM `silent_terminal` trace is still written for a reply-less
  IM Turn end.

## Known by design

- A Run steered in after the Turn's last reply, whose Turn then ends silently,
  settles with empty text and sends no callback: that reply answered an
  earlier input (rule 5).
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
  input's reply both persist as `result`, each pushes once, the Turn ends once
  after both, and the Run settles with the last reply; a silent final after a
  visible reply promotes nothing and the Run settles with that reply; a new
  process ending the Turn settles with this Turn's reply, not a detached
  completion on the same ledger; a replayed earlier reply does not displace a
  later one whose row was never written; each reply has one ledger entry
  across a restart; a Run steered in after the last reply settles empty;
  narration stays `assistant` and leaves the Run text empty; a stop and a
  failure end the Turn once and carry no reply text; IM finalization (running
  indicator, status bubble) happens once, at the Turn end.
- Vibey (owner boundary): a complete reply followed by a steered input commits
  and delivers as `result` and pushes once, the steered reply is a second
  `result`, and the Turn ends once after both.
- Activity grouping, fork completion, and the UI message-role helpers keep their
  rendering for single-reply Turns and treat a mid-Turn reply as a phase
  boundary.
- PR 2: one primary regression per backend at its owning boundary.
