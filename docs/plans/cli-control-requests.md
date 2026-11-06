# Warm CLI control requests: Agent Run

Implementation of Phase 4's first slice of the CLI runtime-path plan (PR #3283).
Performance only; the invocation context is not a security boundary.

## Contract

- `vibe agent run` parses locally, then submits once to the existing private
  control IPC (`POST /internal/control`, Unix socket / Windows descriptor).
  It opens no SQLite connection, including for invocation admission.
- Version 1 carries an operation, parsed arguments, the existing raw
  `CALLER_CONTEXT_ENV_NAMES` fields, the invocation cwd and an admission deadline.
  The controller applies the same command and resource checks. It never mutates
  process-wide environment, cwd or standard streams to impersonate a request.
- The controller's existing Agent and execution stores own enqueue. Blocking
  preparation and state access run on the existing runtime-work executor.
  Sync waits sleep asynchronously and only dispatch bounded reads to threads.
- Existing reservation rollback, P1/P3 intent, Bot routing, source/fork metadata,
  scope and cwd placement, model checks, callback activation, output payloads and
  exit codes remain the owning implementation's behavior.
- A missing/unreachable or older service returns `service_unavailable` with
  `execution: not_executed`. Unsupported/invalid/expired requests are refused
  before mutation. There is no direct-write fallback or automatic service start.
- The deadline is checked before execution, including after executor queueing.
  It does not cancel a started operation. A response timeout, broken connection
  after submission, stale response or malformed response is `execution_unknown`,
  never evidence that enqueue did not happen. There is no automatic retry.
- No schema, operation-id ledger, identity model, Vault custody or deployment
  change. Task/Watch/Session and Vault delivery remain separate follow-ups.

## Validation boundaries

1. Existing Agent Run and Bot tests traverse the request contract and warm
   handler, with only transport replaced. They keep their real stores and all
   existing callback/fork/queue/scope/error assertions.
2. Transport tests cover pre-connect refusal versus post-send ambiguity and
   one send only. A full-main test trips on any SQLite open, including remote
   invocation admission, and observes the forwarded fields.
3. Service tests cover deadline expiry after queueing, concurrent invocation
   isolation, reusable stores, real enqueue and async waiting without loop stalls.
4. A hermetic 200-call, 20-way before/after workload extends the existing phase
   harness with a test-owned control server. No owner state, credentials,
   live-service restart or deployment is part of validation.

Rollback is a code rollback of the command family, not an automatic runtime
fallback. The CLI and service must both include this contract to use the new
path; an older service explicitly refuses instead of making a direct write.
