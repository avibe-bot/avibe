# Agent Message Priority Contract

## Status

Owner-approved program contract. This file freezes the P0, P1, and P3 delivery
semantics and the backend-native steering boundary used by the implementation
slices below. P2 and P4 remain reserved for a separate Mailbox design.

## Priority Semantics

- **P0 interrupt** is an explicit action, never a default for ordinary input.
  Without content it stops the active Turn. With content, the future delivery
  owner must first persist one successor Message, then stop the active Turn and
  start the successor as a new Turn.
- **P1 steer** delivers content into the expected currently active Turn without
  stopping it. A future content-free P1 request atomically promotes the current
  P3 queue head. A definitive steering failure may transfer the same Message to
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

## Delivery Slices

1. **Native adapters (this PR):** add the shared contract and Codex, Claude, and
   OpenCode insertion adapters. Keep every public source and Send Now behavior
   unchanged.
2. **P0/P1/P3 admission owner:** make the Session delivery owner perform the
   durable claim, native steer, refusal-to-P3 transfer, unknown reconciliation,
   content-free P1 promotion, and P0 successor-before-interrupt transition.
3. **Result and callback ownership:** keep one terminal Result authority while
   allowing accepted cross-Agent participants to subscribe once per callback
   Session. A P1-to-P3 transfer joins only the future Turn.
4. **Atomic source cutover:** switch Web Send Now, IM, Agent CLI, Watch, Show
   Annotation, and Session Callback to their approved defaults in one release;
   keep Task at P3 and remove the old Send Now interrupt-and-flush path then.

Until slice 4 lands, Web Send Now, IM handling, CLI defaults, Watch/Task/Show and
Callback routing, `SessionTurnManager.send_now`, queue behavior, and every source
default retain their pre-program behavior. Existing HFR-430 coverage remains the
contract test for Send Now interrupt-and-flush in this slice; no new scenario id
is allocated for this invisible backend capability.

## Deferred Work

- P2/P4 Mailbox cursor, unread, acknowledgement, merge, and retrieval semantics
- generic exact-id promotion or demotion
- source-policy catalog and public surface changes
- callback subscriber persistence and fan-out
- authentication, authorization, ACL, and unrelated issue dependencies
