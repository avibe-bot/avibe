# Agent Message Priority Contract

## Status

Implemented program contract. P0, P1, and P3 share one durable Delivery owner
across interactive input, Harness input, queue promotion, and restart recovery.
P2 and P4 remain reserved for a separate Mailbox design.

## Priority Semantics

- **P0 interrupt** is an explicit action, never a default for ordinary input.
  Without content it stops the active Turn. With content, the delivery owner
  first persists the replacement Delivery and waiting successor Turn, then
  stops the active Turn and starts the successor.
- **P1 steer** delivers content into the expected currently active Turn without
  stopping it. A content-free P1 request atomically promotes the current
  P3 queue head. A definitive steering failure may transfer the same Delivery to
  P3; an acknowledgement-ambiguous result must be reconciled durably first.
- **P3 queue** starts immediately when the Session is idle and otherwise remains
  FIFO until the active Turn ends. Queueing is not a hold mechanism.

Priority controls delivery timing only. A request has no correction/addition
`intent`, and no layer may wrap, prefix, summarize, or otherwise rewrite the
natural-language content.

## Native Steering Contract

The shared backend request has exactly these fields:

```text
SteerRequest
  target_session_id: str
  expected_logical_turn_id: str
  expected_native_turn_id: str
  text: str
```

- `target_session_id` is the target Avibe Agent Session identity.
- `expected_logical_turn_id` is the active Avibe Turn identity observed by the
  caller. It prevents a stale request from steering a later logical Turn.
- `expected_native_turn_id` is the backend-specific active-Turn identity or
  generation observed by the caller: the Codex turn id, the Claude live
  client/receiver generation, or the OpenCode native session/runner generation.
- `text` is the original, unmodified input. Adapters must pass it to the native
  backend byte-for-byte as text, including non-ASCII and Markdown/code content.

The typed result has one `outcome` with exactly four possible values. Optional
`reason` and `details` fields are subordinate diagnostics and do not create
additional outcomes.

```text
SteerResult.outcome = accepted | not_active | refused | unknown
```

- `accepted`: the backend acknowledged insertion into the expected currently
  active Turn.
- `not_active`: no active Turn matches both expected identities.
- `refused`: the backend definitively rejected the request or definitively
  cannot support it. Runtime-unavailable and unsupported states are reasons
  under this outcome when they are known before delivery.
- `unknown`: the transport may have accepted the input, but no trustworthy
  acknowledgement was obtained. Callers must not retry or queue-fallback from
  this outcome without durable reconciliation.

The adapter call is insertion-only. It must not invoke normal new-Turn dispatch,
acquire another Avibe Turn gate, append a primary pending request, replace the
terminal Result owner, create an Agent Run or callback, or install another
receiver.

## Backend Acknowledgements

- **Codex:** call the installed app-server `turn/steer` method with `threadId`,
  `expectedTurnId`, and one text `input`. A response is accepted only when its
  `turnId` equals the expected native turn. Never fall back to `turn/start`.
- **Claude:** call `query()` on the existing live client using the existing
  runtime session id while retaining the same receiver generation. Successful
  completion of the SDK transport write is the acknowledgement. A timeout,
  disconnect, or failed write with an ambiguous partial-write boundary is
  `unknown`; a pre-write unavailable state is `refused`.
- **OpenCode:** call `prompt_async` on the existing native session and runner.
  HTTP 200/204 is the acknowledgement. A definitive HTTP rejection is
  `refused` (or `not_active` when the native session is absent); a timeout or
  disconnect after request dispatch is `unknown`. The adapter never calls abort
  or stop.

These acknowledgement strengths are intentionally different because the native
protocols expose different boundaries. The common result preserves that truth
instead of claiming equivalent durability.

## Source Policy

| Source | Default | Behavior |
| --- | --- | --- |
| Web composer | P3 | Starts when idle; otherwise joins the FIFO backlog. |
| Ordinary IM message | P1 | Steers the active Turn or starts when idle; a definitive refusal falls back to P3. |
| Existing-Session Agent Run | P1 | Same as ordinary IM. `--queue` selects P3 and `--send-now` selects content P0 for Workbench Sessions. |
| Watch, Hook, Webhook | P1 | Continues the target Session without interrupting it. |
| Show annotation | P1 | Delivers the annotation to the current Turn when possible. |
| Session callback | P1 | Delivers each completed child Run independently. |
| Scheduled Task | P3 | Never interrupts or steers the work a user is already doing. |
| Stop | Empty P0 | Stops the exact live Turn. Definitive terminal settlement immediately starts the oldest claimable P3 segment. |
| Send Now existing head | Empty P1 | Promotes only the exact observed FIFO head. |

Attachments remain part of the same Delivery. Because native steer adapters
accept text only, an attachment-bearing P1 is preserved as P3 and starts a new
Turn after the current one finishes.

## Ownership Closure

- `message_deliveries` owns every unaccepted input, queue position, attempt, and
  receipt. No unaccepted input is a transcript Message.
- `session_turns` owns execution and its immutable terminal snapshot.
- `messages` contains accepted communication records only.
- Accepted Agent Runs attached to one Turn are independent terminal subscribers.
  The existing Run rows and their Delivery-to-Turn links are sufficient; no
  callback table or second terminal writer is introduced.
- One terminal snapshot settles every accepted Run once, after which each Run's
  existing callback state produces at most one P1 callback Delivery.

## Deferred Work

- P2/P4 Mailbox cursor, unread, acknowledgement, merge, and retrieval semantics
- generic exact-id promotion or demotion
- authentication, authorization, ACL, and unrelated issue dependencies
