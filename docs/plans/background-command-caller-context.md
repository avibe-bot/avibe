# Background command caller context

## Problem and scope

A Watch correctly bound to Session B ran a Vault CLI subprocess with Session A
inherited from a long-lived service. The Vault request persisted A, so its expiry
callback correctly followed the wrong creation-time identity. Clearing service
provenance (PR2034) alone leaves the waiter without B.

Repair the shared background-command boundary for Watch waiters and scheduled
commands. Keep existing Vault request/callback behavior, definition ownership,
Session policies, resource authorization, and process supervision.

## Contract

- A command's caller Session is its definition's current explicit `session_id`,
  never the service's caller or the definition creator's old Session.
- An unbound command remains unbound. Legacy `session_key` and create-per-run
  definitions do not create or guess a Session just to run a command.
- Remove inherited caller provenance and the transient Session proof before
  spawning, then supply only the current background source, bound Session,
  actual command Run id when one exists, and the definition's stored remote
  authorization snapshot. Never impersonate a human turn or mint Memory proof.
- Remote commands retain remote authority even without a callback Session.
  Existing runtime admission and CLI authorization revalidate that snapshot;
  malformed provenance must not become local-owner authority.
- Keep ordinary process configuration, Watch cursor/delivery variables, and
  process-identity lifecycle unchanged. Other supervised-command consumers keep
  their existing environment behavior.

## Implementation and validation

1. Reuse caller-context sanitization, including removal of its transient proof.
2. Let the generic command runner accept a complete environment; Watch and Task
   producers compose theirs from definition-owned facts.
3. Let CLI authority read remote provenance independently of a callback Session.
4. Test real supervised child processes with a contaminated and clean parent,
   explicit/absent/changed target bindings, both argv and shell forms, unchanged
   ordinary configuration, and remote authority.
5. Drive the child's real Vault CLI context into isolated access-request expiry
   and callback resolution. Use synthetic sealed envelopes, never live secrets,
   Vault requests, grants, or email. Verify the negative control fails before
   the source fix and passes after it.
6. Run focused tests, related Harness/Vault/permission suites, changed Python
   lint, exact-head CI, and automated Codex review through one durable Watch.

## Known by design

- No historical requests are rewritten or replayed.
- This source PR does not upgrade/restart the running host or resume failed
  automation. Released-host verification requires separate authorization.
- A command without a bound Session cannot automatically resume an Agent
  through Vault; explicit CLI targeting remains available.
- The callback consumer and credential grants are not changed to compensate
  for bad producer identity. No new storage schema or alternate routing owner.

## Local acceptance

HFR-486 reproduced both original failures before the change. The accepted
related suites passed 1,463 tests; four skips are two Windows-only worker
contracts on macOS and two Organization-only denial shapes in the Personal
instance matrix. Pinned Ruff 0.4.9 passed. These are isolated source/process
tests, not a running-host upgrade or live provider/credential trial.
