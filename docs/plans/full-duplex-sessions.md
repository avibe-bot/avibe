# Full-duplex Session implementation contract (#862)

## Goal

Make the durable Session message stream independent from foreground execution
ownership while preserving `SessionTurnManager` as the single owner of
foreground queueing, Stop, and completion.

The implementation spans the shared dispatcher, durable Activity recovery,
Claude background-task output, Harness Run callbacks, and Workbench projection.
It does not introduce a global Session FSM or a speculative public schema.

## Shared contracts

### Message output

`core.message_output.MessageOutput` accompanies every live agent output whose
lifecycle authority must be explicit:

- `completes_turn`: whether this output is a terminal foreground signal;
- `completes_run`: optional independent Run terminal signal;
- `detached`: whether it is legitimate output from work whose foreground Turn is
  already over; detached output can be delivered but cannot mutate another Turn;
- `idempotency_key`: stable producer identity for delivery/persistence dedup;
- `activity_id`, `causation_id`, and `sequence`: hidden provenance.

The dispatcher makes delivery and lifecycle decisions separately. A detached
output follows normal cleaning, persistence, delivery, and Session fan-out, but
does not settle the dot, stream sink, runtime gate, processing indicator, status
bubble, or a newer Turn. It may still settle its originating Run when
`completes_run=True` and no non-detached owned Activity remains active.

Live core and backend paths no longer infer lifecycle authority from the visible
`result` role. One dispatcher-boundary fallback keeps older external callers
compatible while they migrate. A backend can emit multiple result-shaped output
Messages by using `completes_turn=False` for intermediate outputs and one
terminal output.

### Activity registry and restart recovery

`core.session_activities.SessionActivityRegistry` owns backend-neutral
operational state. Backends report independently identified Activity
start/progress/terminal events and connection changes. The registry projects,
without a cross-product enum:

- active background Activities;
- backend connection state;
- completed Activities waiting for a producer-owned follow-up output.

Restart snapshots use the existing `runtime_records` aggregate rather than a new
Activity table. An active Activity snapshot, a completed Activity waiting for
output, and backend connection state are persisted independently. Completed
output is removed only after its delivery policy is acknowledged.

On controller restart:

- active native work becomes `disconnected`; any owned Run reaches its existing
  failure/cancellation policy exactly once;
- completed output remains pending with its stable producer identity;
- a stored summary is delivered once as detached Session output, without
  lifecycle authority over any newer Turn;
- if no summary or valid Session route exists, the Run settles silently and no
  user-visible text is invented;
- every recovered native connection projects as `disconnected` until the backend
  reconnects.

`SessionTurnManager.turn_state()` composes this with its existing foreground
state and the durable queued-message count. The Workbench API and UI consume the
orthogonal `foreground`, `pending_input_count`, `background_activities`,
`pending_activity_output_count`, and `connection` facts. `in_flight` remains a
read-only compatibility alias for older clients, not the Session state model.

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
Deferred terminal intent is excluded from generic restart requeueing, then
reconciled against recovered Activity/output snapshots before normal Run drain.

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
newer pending native request. Queued completions survive both runtime disconnect
and controller restart so a late flush can still deliver and settle their origin
Run.

## Other backend protocol disposition

The shared contracts are used by Claude, Codex, and OpenCode, including explicit
terminal output authority. Native Activity mapping remains capability-driven:

- Codex app-server currently exposes thread/turn/item events scoped to an active
  Turn, but no independently identified unit of work that can complete after
  that Turn;
- OpenCode polling exposes Session messages/tool parts plus Session idle/error,
  but no independently identified background work with its own post-Turn
  lifecycle.

Inventing Activities from ordinary tool or Turn events would freeze the wrong
schema and conflate foreground execution with background agency. These backends
therefore keep serialized accepted Inbox work today and will inherit native
Activity mapping when their protocols expose a stable independent identity and
lifecycle.

## Compatibility and non-goals

- No schema migration: Message provenance uses existing `metadata_json`, the Run
  ledger uses existing `result_payload_json`, and restart snapshots use existing
  `runtime_records`.
- No new Session enum and no replacement of `SessionTurnManager`.
- No concurrent-inference requirement. Existing queueing remains the fallback.
- No automatic user visibility for backend progress/tool frames.
- No classification of ordinary Codex/OpenCode Turn or tool events as fake
  Activities.
- No removal of the quarantined dispatcher compatibility fallback in the same
  change; all live runtime paths already use explicit output semantics.

## Acceptance evidence

- `MESSAGE-DELIVERY-003`: Claude background completion is delivered while a
  newer Turn remains active.
- `MESSAGE-DELIVERY-004`: one Turn emits multiple output Messages and completes
  once.
- `MESSAGE-DELIVERY-005`: one Run forwards multiple callback outputs and reaches
  one idempotent terminal transition.

Focused unit/contract tests cover Activity transitions, restart recovery,
summary and no-summary policies, state-axis projection, structured provenance,
dedup, terminal isolation, Run/callback settlement, and existing one-result
compatibility across Claude, Codex, OpenCode, dispatcher, scheduler, and
Workbench paths.
