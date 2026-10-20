# A reply is not the end of a Turn

Status: approved by the owner (cyhhao), 2026-10-19. PR 1 (merged): shared
contract, Vibey, and Claude's pre-steer phase typed as a reply. PR 2a (merged):
Codex and OpenCode on the reply rule, and rule 5 owned by the dispatcher. PR 2b
(merged): typed output events. PR 3: the presentation decision point. Later: see
Next steps.

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
   The dispatcher, which writes the delivered Run copy, owns this (PR 2a): when
   it delivers a reply that does not end the Turn, it records that reply's Run
   copy for the durable Turn (`session_turn_replies`, one row per Turn), and a
   done Turn end with no reply of its own settles the Turn snapshot and every
   attached Run with it. Producers forward nothing and nothing reconstructs it.
   Rule 5 is best effort, not exactly-once (an IM send is an external effect no
   local record can commit with), with one degradation policy: the local
   receipt is the record, written atomically with the reply's receipt row, so
   when that row's write fails after an IM send the transcript and the record
   both lack the reply and the end settles with the previous recorded reply, or
   ""; a read of the record that still fails after a short bounded retry is
   logged and read as no reply, so the end settles with "" rather than holding
   or failing the Turn's end.
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
- A Turn end that carries text settles its Runs and writes the Turn snapshot
  with it. A done Turn end with no text of its own (a silent final, an empty
  end, a footer-only end) settles them with the Turn's row in
  `session_turn_replies`, read by primary key off the loop, or with "" when the
  Turn delivered no reply (rule 5). Failed, stopped and teardown ends never
  read it.
- The write: a reply that does not end its Turn records its Run copy (the same
  copy the Runs get: `process_reply` applied, citations written out) for the
  live Turn in the same SQLite transaction that writes its receipt, the row
  `persist_agent_message` writes (on the Web its delivery, on IM once the send
  returned, in a background Session its local row, for Vibey the display write
  into its committed row). The receipt exists exactly when the record does: a
  record that cannot be written fails the receipt the way a failed row write
  does, and a crash between an IM send and its row write leaves neither, so a
  replay sends again, as before. It is one upsert keyed by the Turn's id,
  guarded by the Turn being `starting` or `active`, in a table of its own
  (`turn_id` → `session_turns.id`, deferred, `ON DELETE CASCADE`), so it never
  takes part in the Turn's compare-and-set and no transition writes or clears
  it. A replay that finds its receipt records nothing, so an earlier reply never
  displaces a newer one; narration, notices, failures, detached and Activity
  outputs never write it. The last delivered reply wins, so A, B, A settle with A.
- Every write derived from a reply that does not end its Turn happens only at
  the dispatcher's delivered-reply point, once delivery evidence exists (on the
  Web and in a background Session its receipt row persisted, on IM the platform
  accepted the send): its Run output entry, its record as the Turn's last reply
  (committed with the receipt), and a backend's own mark (OpenCode's emitted
  id, from the emit's returned message id). Finals and Activity outputs keep
  their settlement order. The emit's return value is that evidence: a
  background output returns its local row's id, never a synthetic one, and
  nothing when the row was not written. The Run output entry is best effort
  under rule 5's policy: it is written in its own step after the receipt, so a
  crash between the two can leave it out of `result_payload.outputs`, and a
  replay never backfills it.
- Every reply carries the identity its backend gives it as `idempotency_key`
  (OpenCode's message id, Codex's item id, Vibey's committed row id, Claude's
  `ResultMessage.uuid`), so two replies with the same text stay two entries in
  a Run's `result_payload.outputs` and a replay stays one. A Claude phase
  rebuilt from assistant frames at a steer receipt has no Result and so no
  identity; it falls back to its text.
- A footer is never a reply: with nothing visible to say, `base.emit_result_message`
  emits an empty end and passes the duration footer only as `result_footer`,
  so a footer-only end is content-free.
- A Run's `result_payload.outputs` stays its append-only delivery log: every
  reply records its Run output when it is delivered, Claude's pre-steer phase
  included.
- A failed or stopped Turn reports its outcome, never an earlier reply, and a
  `level="silent"` end that carries text still records that text, as before.
- Claude: its completed pre-steer phase is now a non-terminal `result` that
  records its Run output.
- Codex (PR 2a): `agentMessage.phase` decides. `final_answer` and
  `partial_answer` are reply candidates; once anything follows a held candidate
  (another answer, commentary, a tool call), it is delivered first, in turn
  order, as a `result` that does not end the Turn. `commentary` is narration. A
  message with no phase keeps the last-visible rule: a visible item after it
  (a message, commentary, a tool call) makes it narration, delivered first in
  the provider's order. The last
  candidate rides the `turn/completed` end, and a silent answer never displaces
  a visible one.
- OpenCode (PR 2a): every completed assistant message whose finish is not
  `tool-calls` is a reply. Earlier ones are delivered at settlement as
  `result`s that do not end the Turn; the last visible one rides the end, and a
  silent final never displaces it.
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
| Run `result_text` / callback on a silent final | empty | PR 2a: the Turn's last delivered reply (`session_turn_replies`), "" when none |
| Turn snapshot `result_text` on a silent final | empty | PR 2a: as the Run |
| Footer-only end (`show_duration`, no reply) | a green `result` row holding the footer, pushed, unread; Runs settle with the footer | PR 2a: content-free; no row, the concise status bubble keeps its done line, Runs settle as rule 5 says |
| Codex earlier `agentMessage` (`codex/event_handler.py`) | narrated as `assistant` | PR 2a: `result` when its phase is an answer; `commentary` narration; no phase unchanged |
| OpenCode superseded reply (`opencode/poll_loop.py`) | `assistant` | PR 2a: `result` |
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

PR 2a: Codex narration of an earlier answer and OpenCode's superseded replies
as `assistant`; the base result path's footer promotion.

## Compatibility

- Rows written by released versions keep their type: `output` renders muted,
  `interim` renders muted, a promoted `result` (`metadata.promoted_from`) keeps
  its recorded Activity role. `PROMOTED_FROM_KEY` stays readable; nothing
  writes it any more.
- `committed_final` still treats a stored `output` as not final.
- PR 1: no schema or index change; `activityRole` is not part of any partial
  index. PR 2a: migration 0071 adds the table `session_turn_replies`
  (additive). Released versions never read it; a database they wrote upgrades
  with every Turn reading as having delivered no reply, and downgrade drops the
  table.
- IM Turns have no durable Turn row; a visible IM reply closes its Activity
  group, and the IM `silent_terminal` trace is still written for a reply-less
  IM Turn end.

## Known by design

- User-visible (PR 2a): a Turn that ends with no reply while `show_duration`
  is on no longer posts a footer-only message (Web or IM verbose mode). The
  Turn still visibly ends: the thinking indicator hides on `turn.end`, the IM
  running reaction clears, and the concise status bubble collapses to its done
  line with the run time.
- A reply's Run output entry is best effort: it is written after the reply's
  receipt, in its own step, so a crash between the two leaves it out of the
  Run's `result_payload.outputs`; a replay finds the receipt and does not
  backfill it.
- Rule 5 is best effort (rule 5's degradation policy): an IM reply whose row
  write fails after its send has no receipt and no record, so the Turn settles
  with the reply before it; a record read that keeps failing settles with "".
- A reply whose every send failed is not retried (the dispatcher posts its
  delivery-failed notice) and the Turn ends normally. OpenCode records a reply
  as emitted only once the dispatcher returned delivery evidence, so a restored
  poll still owes an undelivered one; Codex keeps no emitted state for a
  flushed reply, so nothing marks a failed one delivered.
- A Turn with no durable row (legacy and CLI paths) has no recorded reply, so
  its content-free end settles with empty text as before.
- A Turn that ended content-free lists its last reply twice in a Run's
  `result_payload.outputs`: once as delivered, once as the settlement entry
  under the terminal identity, as every final's terminal record is.
- Codex `commentary` that ends a Turn is narration, so such a Turn has no reply
  of its own and settles as rule 5 says.
- On the Web the thinking indicator stays until `turn.end`, so it can show
  briefly under a final reply while the runner commits the Turn.
- In IM a reply that does not end the Turn is its own message without the done
  footer; the status bubble keeps running and is retired at the Turn end, as a
  phase output was.
- IM Turns have no durable running state, so a fork of an IM source taken
  mid-Turn reads a reply after its anchor as completion, as it read a `result`.

## Test plan

Scenarios `MESSAGE-DELIVERY-334` (PR 1) and `MESSAGE-DELIVERY-335` (PR 2a)
(`tests/scenarios/message_delivery/catalog.yaml`).

- Dispatcher (owner boundary): a non-terminal reply followed by the steered
  input's reply both persist as `result`, each pushes once and is on the Run's
  output log, and the Turn ends once after both and settles with the last reply;
  narration stays `assistant` and leaves the Run text empty; a stop and a
  failure end the Turn once and carry no reply text; IM finalization (running
  indicator, status bubble) happens once, at the Turn end.
- Rule 5 (PR 2a, dispatcher with the real Turn row and Run store): every
  content-free done end (silent final, empty end with the footer shown or
  hidden, a bare end), on the Web and IM, adds no row and settles the snapshot
  and every attached Run, a steered one included, with the written copy of the
  last delivered reply (A after A, B, A), read by an end in a new process; a
  reply whose send failed is skipped, and a Turn that delivered none settles
  with ""; a footer-only end posts nothing and still collapses the concise
  bubble, finishes the running indicator, and clears the reaction. Storage: a
  reply write and a real Turn transition both land, the write and read are
  primary-key SEARCHes, and a released database upgrades (its running Turn
  ends content-free with ""), downgrades on SQLite before 3.35 too, and
  upgrades again.
- Codex and OpenCode (PR 2a): an answer a steer follows stays a `result` that
  does not end the Turn, in turn order; `commentary` is narration.
- Vibey (owner boundary): a complete reply followed by a steered input commits
  and delivers as `result` and pushes once, the steered reply is a second
  `result`, and the Turn ends once after both and settles with the last reply;
  a rerun after a restart promotes no stored `output` and settles with the
  reply the dead process delivered.
- Claude: a completed pre-steer phase emits a non-terminal `result` that
  records its Run output.
- Activity: the terminal-Turn read is pinned to an index-ordered SEARCH with no
  temporary sort.
- Activity grouping, fork completion, and the UI message-role helpers keep their
  rendering for single-reply Turns and treat a mid-Turn reply as a phase
  boundary.
- PR 2b: the event-to-dispatcher mapping is table-tested (`MESSAGE-DELIVERY-336`);
  every existing suite passes with only mechanical updates (canonical options on
  asserted calls, receipts instead of raw ids).

## Next steps

Approved by the owner, in this order:

1. PR 2, split by the orchestrator into two PRs:
   - PR 2a (behavior, with migration 0071; merges only with the owner's
     approval): Codex and OpenCode move to the reply rule (Claude and Vibey
     are on it since PR 1), and the dispatcher owns rule 5 through
     `session_turn_replies`. Requirements found in PR 1's review:
     only a reply whose delivery succeeded counts; the text is the one the
     dispatcher wrote (quick replies and file links stripped, citations
     written out), not the backend's source text; it holds across a restart
     rerun and startup reconciliation, under repeated identical replies, and
     for every Run attached to the Turn, one a steer attached after the reply
     included. A footer-only end is content-free.
   - PR 2b (structural only, no behavior change, branched from master after
     2a merges): every backend, Vibey included, reports output through one
     typed event set (`core/agent_output.py`): reply, narration, tool activity,
     turn end (outcome), error, and notice for the rest of a backend's visible
     notices. Vibey's generated-images fallback keeps its released `output` type
     on the legacy entry until the presentation decision point (PR 3). This replaces type strings plus `MessageOutput` flags, so the
     dispatcher no longer infers semantics. Vibey's agent-event schema is the
     starting point, and rule 5 moves onto the typed Turn-end event. Emits
     return a typed delivery receipt (delivered, with its receipt row or the
     platform's message id; or not delivered) instead of an id or `None`, so
     delivery evidence is a type rather than a convention every caller reads.
2. PR 3, structural only with no behavior change. One presentation decision
   point per output event: transcript or not, muted, push, IM delivery.
   Today's rules move into it unchanged as the default policy: the interim
   length threshold, hidden types, IM concise mode, `suppress_delivery`. The
   decision is recorded on the message. Shape (`core/presentation.py`): the
   dispatcher builds a `PresentationInput` per output (its type, `level`,
   target platform, background, body, and, read only when the policy asks,
   the channel's hidden types and the turn's progress style), asks its
   `presentation_policy`, and executes the `PresentationDecision` (`persisted`,
   `transcript`, `muted`, `web_push`, `outward`, `im`: none, status, log or
   message). `DefaultPresentationPolicy` (`default/1`) is the only policy; a
   later one (a decision model, or one that wraps the default) replaces it
   through the same protocol. Each message records the decision under
   `metadata["presentation"]` (additive; released readers ignore it). Vibey's
   generated-images fallback goes through it with its released `output` type.
3. PR 4, structural only with no behavior change. One admission entry for
   every input source: Web composer, IM, Harness, Show annotation, failure
   retry, agent-initiated. Each input is normalized to sender, kind and
   content, and its priority is decided through one default policy that holds
   today's per-source defaults. Who decided is recorded in the delivery
   history.

Out of scope for now: Mailbox, P2 (doorbell, owner-confirmed meaning, not
specified yet) and P4, decision models, firewall rules.
