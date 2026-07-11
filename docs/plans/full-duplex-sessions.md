# Full-duplex Session implementation contract (#862)

## Goal

Make Session message flow independent from foreground execution ownership while
preserving the existing `SessionTurnManager` as the single owner of foreground
queueing, Stop, and completion.

This change is a vertical slice across the shared dispatcher, backend activity
reporting, Claude background-task output, and Harness Run callbacks. It does not
introduce a global Session FSM or a new persistence aggregate.

## Shared contracts

### Message output

`core.message_output.MessageOutput` accompanies an agent-visible output when its
lifecycle differs from the compatibility default (`result` completes the current
Turn):

- `completes_turn`: whether this output is a terminal foreground signal;
- `completes_run`: optional independent Run terminal signal (defaults to the
  legacy `completes_turn` behavior);
- `detached`: whether it is legitimate output from work whose foreground Turn is
  already over; detached output can be delivered but cannot mutate another Turn;
- `idempotency_key`: stable producer identity for delivery/persistence dedup;
- `activity_id`, `causation_id`, and `sequence`: hidden provenance.

The dispatcher makes delivery and lifecycle decisions separately. A detached
output follows normal cleaning, persistence, delivery, and Session fan-out, but
does not settle the dot, stream sink, runtime gate, processing indicator, status
bubble, or a newer Turn. It may still settle its originating Run when
`completes_run=True` and no non-detached owned Activity remains active.

Existing callers remain compatible: an ordinary `result` still completes its
current Turn. A backend can emit multiple result-shaped output Messages by using
`completes_turn=False` for intermediate outputs and one terminal output.

### Activity registry

`core.session_activities.SessionActivityRegistry` is process-local operational
state, not a new durable domain table. Backends report independently identified
Activity start/progress/terminal events and connection changes. The registry
projects, without a cross-product enum:

- active background Activities;
- backend connection state;
- completed Activities waiting for a producer-owned follow-up output.

`SessionTurnManager.turn_state()` composes this with its existing foreground
state and the durable queued-message count. Existing response fields remain
unchanged.

### Run outputs and callbacks

The existing `agent_runs.result_payload_json` stores an idempotent output ledger:

```json
{
  "outputs": [
    {
      "id": "producer-stable-id",
      "text": "clean user-visible output",
      "message_id": "optional delivery id",
      "sequence": 1,
      "provenance": {"activity_id": "...", "run_id": "..."}
    }
  ]
}
```

No visible wrapper text is added. Each new output can enqueue one callback turn
immediately. Callback turns are deduplicated by structured Run lineage. Parent
`callback_status` stays pending while the parent Run is active and settles once
after the parent reaches its one idempotent terminal transition.

If a Run fails or is canceled after forwarding partial outputs, its callback
Session receives one additional terminal failure/cancellation Message. A
successful Run with streamed outputs does not repeat them in a synthetic final
summary.

A terminal Run intent is retained in `result_payload_json` while a non-detached
owned Activity remains active. A later Activity output can complete that Run
without acquiring lifecycle authority over whichever foreground Turn is current.

## Claude mapping

Claude task frames map into the shared Activity registry:

- `task_started`: start/upsert by `task_id`;
- `task_progress`: refresh by `task_id`;
- terminal `task_notification` or `task_updated`: complete exactly that Activity
  for `completed`, `failed`, `stopped`, or `killed`.

Typed SDK frames are used where available; raw `SystemMessage.subtype/data` is
the forward-compatible fallback. A foreground `ResultMessage` does not clear
active background Activities. When a completed Activity produces a later
assistant/result sequence while another user Turn owns the runtime gate, Claude
delivers only the final user-facing result as a detached Message output. The
newer user Turn remains untouched.

The Claude stream does not expose a reliable query/result correlation id. Avibe
therefore accepts the next Session input normally but serializes that native
query while a background Activity or its undelivered completion can still
produce output. This is backend execution admission, not Session message
admission. A terminal-only task notification is delivered after a bounded grace
period; a timed flush never consumes Activity provenance from underneath a
newer pending native request. Queued completions also survive runtime disconnect
inside the process so a late flush can still deliver and settle their origin Run.

## Compatibility and non-goals

- No schema migration: Message provenance uses existing `metadata_json`; the Run
  ledger uses existing `result_payload_json`.
- No new Session enum and no replacement of `SessionTurnManager`.
- No concurrent-inference requirement. Existing queueing remains the fallback.
- No automatic user visibility for backend progress/tool frames.
- Codex and OpenCode inherit the shared output and Activity APIs; they need no
  adapter-specific Activity mapping until their native protocols expose such
  work.

## Acceptance evidence

- `MESSAGE-DELIVERY-003`: Claude background completion is delivered while a
  newer Turn remains active.
- `MESSAGE-DELIVERY-004`: one Turn emits multiple output Messages and completes
  once.
- `MESSAGE-DELIVERY-005`: one Run forwards multiple callback outputs and reaches
  one idempotent terminal transition.

Focused unit/contract tests cover Activity transitions, state-axis projection,
structured provenance, dedup, terminal isolation, and existing one-result
compatibility across shared dispatcher paths.
