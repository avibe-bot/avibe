# Session Delivery Foundation

## Goal

Replace the mutable pseudo-Message queue with one durable model that remains
coherent before dispatch, during a native Turn, and after acceptance.

## Ownership

- `message_deliveries` owns submitted content until native acceptance. A queued
  Delivery remains independently removable.
- `session_turns` owns one native dispatch, including its start attempt, exact
  merged dispatch text, ordered initial Delivery batch, control slot, runtime
  identity, and terminal snapshot.
- `messages` owns accepted communication only. One accepted initial batch
  materializes one Message; every Delivery in that batch links to it.
- `agent_sessions` owns archive state, queue hold, and composer draft.
- `agent_runs` may reference the Delivery created for that Run. It does not own
  queue order and never binds directly to a Message. Each Run has at most one
  Delivery, while one Turn may accept several Deliveries and therefore several
  Runs.

Once a Run is bound to a Delivery, the Delivery/Turn owns its completion. A
producer returning from admission is not a terminal event: a queued Delivery
keeps its Run nonterminal, exact queue removal cancels both, and accepted work is
settled only from the Turn's terminal evidence.

## Delivery State Matrix

One runtime matrix defines the meaning of every Delivery state. It records five
orthogonal facts: FIFO role, whether native work may have happened, whether the
snapshot can still reach dispatch, and whether Run cancellation may retire the
Delivery directly, plus whether durable admission has completed. Queue claiming,
Run cancellation, Agent archive rewriting, Show admission, and recovery derive
their state sets from those facts; they do not maintain local state lists.

| State | Ordering | Native effect | May dispatch | Run cancel | Submission |
| --- | --- | --- | --- | --- | --- |
| `reserved` | fence | none | yes | retire | reserved |
| `queued` | claimable | none | yes | retire | admitted |
| `claimed` | Turn-owned | possible | yes | Turn owner | admitted |
| `pending_steer` | fence | none | yes | retire | admitted |
| `steering` | fence | possible | yes | Turn owner | admitted |
| `interrupt_waiting` | Turn-owned | possible | yes | Turn owner | admitted |
| `reconciling_steer` | fence | unknown | yes | Turn owner | admitted |
| `reconciling_migration` | fence | unknown | no | Turn owner | admitted |
| `accepted` | terminal | accepted | no | complete | admitted |
| `retired` | terminal | none | no | complete | retired |

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

Unknown start or steer outcomes never retry. A definitive pre-write start failure
requeues the entire claimed batch in its original order. A late event for T1
cannot mutate T2.

OpenCode writes the persisted Delivery attempt ID as the native `messageID`.
After restart, reconciliation reads that exact native Message without issuing a
second prompt: exact presence accepts, while absence or transport uncertainty
remains blocked.

Terminal settlement, the next compatible FIFO claim, and the Session status
projection share one writer transaction. Result evidence is committed only after
output persistence/delivery has settled. A post-native storage failure therefore
keeps the prior Turn and running projection for recovery instead of exposing an
idle gap.

Every backend write must first persist enough identity to query that exact
attempt after restart. OpenCode persists its poll/recovery address before the
prompt call and restores even an already-completed exact start, so recovery can
materialize and settle it without replaying native work.

Turn-scoped consumers never infer the current input from transcript timestamps.
They resolve `session_turns.initial_delivery_id`: before acceptance they read the
Delivery snapshot, and afterward they read its linked Message.

## Product Policy

- Web user input is P3; while busy it joins the removable backlog.
- existing-Session Agent Run and Watch input is P1 by default; it steers the
  active Turn, starts itself when idle, and falls back to P3 only after a
  definitive refusal or not-active receipt;
- explicit send-now/content replacement is P0;
- empty send-now promotes the exact current P3 head segment;
- only empty P0/Stop establishes the durable backlog hold;
- content P0 preserves the existing hold and its replacement Turn bypasses it.

On startup, OpenCode first restores the exact logical/runtime/native identity
map. Restored polling then waits until durable Delivery/Turn reconciliation has
finished, so neither side can consume the other's evidence early.

## Migration

Revision 0044 follows the already-shipped 0043 migration. It creates
`message_deliveries` and `session_turns`, moves unaccepted pseudo Messages into
Deliveries, and leaves accepted communication Messages as immutable history.
`/new` archives a non-empty Session and re-anchors future work; only empty
Sessions are physically removed, so accepted Messages and their Delivery/Turn
audit graph remain intact.

## Delivery Gate

The implementation ships as one authority cutover. No producer may create
queued, pending, draft, dedupe, silent, or tool-call pseudo Messages. No runtime
path may merge Agent Runs before the Turn claim. Review stops before another push
when one root-cause class appears on a second reviewed head, or when three
findings-bearing heads follow this rewrite without a clean pass.
