# Session Delivery Foundation

## Goal

Replace the mutable pseudo-Message queue with one durable model that remains
coherent before dispatch, during a native Turn, and after acceptance.

## Ownership

- `message_deliveries` owns submitted content until native acceptance. A queued
  Delivery remains independently removable. Its submission snapshot is
  immutable: Agent rename/archive and later routing changes never rewrite it.
- `session_turns` owns one native dispatch, including its start attempt, exact
  merged dispatch text, ordered initial Delivery batch, control slot, runtime
  identity, and terminal snapshot.
- `messages` owns accepted communication only. One accepted initial batch
  materializes one Message; every Delivery in that batch links to it.
- `agent_sessions` owns archive state and composer draft. Queue liveness is
  derived: an active Session with no live Turn and a claimable FIFO head is
  runnable, so no independent hold state can leave queued work idle by policy.
- `agent_runs` may reference the Delivery created for that Run. It does not own
  queue order and never binds directly to a Message. Each Run has at most one
  Delivery, while one Turn may accept several Deliveries and therefore several
  Runs.

Once a Run is bound to a Delivery, the Delivery/Turn owns its completion. A
producer returning from admission is not a terminal event: a queued Delivery
keeps its Run nonterminal, exact queue removal cancels both, and accepted work is
settled only from the Turn's terminal evidence.

## Delivery State Matrix

One runtime matrix defines the meaning of every Delivery state. It records four
orthogonal facts: FIFO role, whether native work may have happened, whether Run
cancellation may retire the Delivery directly, and whether durable admission
has completed. Queue claiming, Run cancellation, Show admission, and recovery
derive their state sets from those facts; they do not maintain local state
lists.

| State | Ordering | Native effect | Run cancel | Submission |
| --- | --- | --- | --- | --- |
| `reserved` | fence | none | retire | reserved |
| `queued` | claimable | none | retire | admitted |
| `claimed` | Turn-owned | possible | Turn owner | admitted |
| `pending_steer` | fence | none | retire | admitted |
| `steering` | fence | possible | Turn owner | admitted |
| `interrupt_waiting` | Turn-owned | possible | Turn owner | admitted |
| `reconciling_steer` | fence | unknown | Turn owner | admitted |
| `accepted` | terminal | accepted | complete | admitted |
| `retired` | terminal | none | complete | retired |

An empty send-now request also carries the exact Delivery ID observed by its
caller. The writer transaction may promote only that ID if it is still the FIFO
head; it never substitutes a newer head.

## Queue Batching

Queued Deliveries remain separate until a Turn is claimed. The claim transaction
selects one compatible leading segment, records every member and its position on
the Turn, records the exact merged prompt, and only then permits one backend call.

Before claim, removing B from `[A, B]` retires only B. After `[A, B]` is claimed,
Stop interrupts their shared Turn; neither submission can be withdrawn alone.

Compatibility follows the existing segment rules:

- adjacent ordinary user inputs may merge;
- native-targeted P1 attempts remain separate before any refusal;
- scheduled inputs that have fallen back to P3 may merge only under their
  existing definition, trigger-kind, and rolling-window rules;
- P1 steering never consumes a P3 backlog segment. A definitively refused P1
  may fall back to P3 and later participate under normal queue rules.

## Acceptance

Positive native start evidence atomically creates one merged inbound Message and
links every initial Delivery to that Message and Turn. Original submission times
stay on Deliveries; the Message `created_at` is the first Delivery's submission
time and `delivered_at` is the acceptance time. Positive steer evidence
materializes that single Delivery as a participant Message on the exact Turn,
including after the Turn became terminal.

The Turn's persisted `start_receipt_outcome='accepted'` is the only gate for
initial Message materialization. Terminal output alone cannot prove that native
input was accepted: without the start receipt, the Turn remains owned and
blocked for reconciliation rather than exposing a phantom Message or idle gap.

Unknown steer outcomes never retry. For Claude and Codex, an unresolvable start
after restart replays the same Delivery batch once with a private instruction to
check existing effects before irreversible work. A second unknown attempt retires
that batch and releases the Session. OpenCode retains its stronger exact-attempt
reconciliation. A definitive pre-write start failure requeues the entire claimed
batch in its original order. A late event for T1 cannot mutate T2.

OpenCode generates every native `messageID` at the actual write and stores the
persisted Delivery attempt ID as the exact ID of the user Message's text part.
After restart, reconciliation lists that native Session's Messages without
issuing a second prompt: an exact user-part match accepts and returns its native
Message ID, while absence or transport uncertainty remains blocked.

Terminal settlement, the next compatible FIFO claim, and the Session status
projection share one writer transaction. Result evidence is committed only after
output persistence/delivery has settled. A post-native storage failure therefore
keeps the prior Turn and running projection for recovery instead of exposing an
idle gap.

Every backend write must first persist enough identity to query that exact
attempt after restart. OpenCode persists its poll/recovery address before the
prompt call and restores even an already-completed exact start, so recovery can
materialize and settle it without replaying native work.

An OpenCode verification error is unknown evidence, not proof that a poll is
stale. Startup preserves and restores that durable poll until exact positive or
definitive-negative evidence is available.

Turn-scoped consumers never infer the current input from transcript timestamps.
They resolve `session_turns.initial_delivery_id`: before acceptance they read the
Delivery snapshot, and afterward they read its linked Message.

Delivery provenance never selects the Agent that executes later work. Dispatch
rebuilds routing from the current Session and its stable `agent_id`; historical
Agent names remain immutable attribution only. Agent archive or rename updates
live Session/Run bindings and cannot mutate an unaccepted Delivery snapshot.

## Product Policy

- Web user input is P3; while busy it joins the removable backlog.
- existing-Session Agent Run and Watch input is P1 by default; it steers the
  active Turn, starts itself when idle, and falls back to P3 only after a
  definitive refusal or not-active receipt;
- Agent Run send-now persists its new input at P3, then promotes the exact
  current P3 head segment through empty P1, so older work cannot be bypassed;
- Session send-now performs the same exact-head P1 promotion without adding
  content;
- P0 is reserved for explicit content-free Stop.

On startup, OpenCode first restores the exact logical/runtime/native identity
map. Restored polling then waits until durable Delivery/Turn reconciliation has
finished, so neither side can consume the other's evidence early.

## Migration

Revision 0044 follows the already-shipped 0043 migration. It creates
`message_deliveries` and `session_turns`, moves unaccepted pseudo Messages into
Deliveries, and leaves accepted communication Messages as immutable history.
Legacy `pending` rows have no recoverable native identity, so migration retains
their snapshot as a retired, not-replayed Delivery instead of creating a FIFO
fence that can never resolve.
`/new` archives a non-empty Session and re-anchors future work; only empty
Sessions are physically removed, so accepted Messages and their Delivery/Turn
audit graph remain intact.

## Delivery Gate

The implementation ships as one authority cutover. No producer may create
queued, pending, draft, dedupe, silent, or tool-call pseudo Messages. No runtime
path may merge Agent Runs before the Turn claim. Review stops before another push
when one root-cause class appears on a second reviewed head, or when three
findings-bearing heads follow this rewrite without a clean pass.
