# Warm CLI control requests: Agent Run and Vault fetch

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
  File inputs are materialized by the CLI (including stdin/process substitution);
  an input error is delivered only after the existing admission/target checks.
  An unavailable invocation cwd is explicit and only refused when actually needed.
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
  change. Task/Watch/Session mutations remain a separate follow-up.

## Vault fetch change contract (PR B)

The hot path is one `vault.fetch` request on the same private control channel.
The invoking process remains the owner of input files, output files and standard
streams; it materializes request bytes, not filenames, before IPC. Admission
still precedes reporting a prepared local input error. The controller uses its
cached engine and existing policy/grant/delivery helpers. It forwards only the
sealed envelope to avault and returns only the HTTP result, never credentials.
The caller's raw invocation environment and PID supply the same context and
audit metadata as the preceding CLI implementation; globals are never swapped.

Approval is the cold path. Only a resolver-originated `approval_required`
response with an explicit `not_executed` approval envelope permits
`vault.fetch.await`, which owns the existing approval waiter lock and
continuation transitions. It sleeps asynchronously and runs bounded store steps
off-loop. A disconnected, still-active waiter releases its lock without handing
off a fetch, so the existing callback can resume the Session. Once an approval
is consumed, the caller rechecks its output file and submits fetch once, using
the original materialized body and canonical secret record. Host/auth policy is
read again. This uses the existing approval request ID, not a new operation ID,
registry, schema or retry protocol. An uncertain fetch result is never replayed.
A disconnect observed before queued completion obtains its write reservation
leaves the active waiter recoverable by the callback. If completion has already
committed and its reply is lost, however, neither an automatic fetch nor callback
follows; the client must not reopen that decision. This extends the preceding
CLI's completion-to-handoff crash window across IPC and client output checks.
A service restart also ends its waiters; a surviving CLI does not reconnect or
retry. The approval notice now appears before the owning await RPC, rather than
after its arm. `vault run` retains its existing in-process wait path.

Request/response JSON codecs and body decoding run off-loop as well as database
and egress work. Ordinary one-shot cleanup reuses the cached service engine;
exceptional resident-agent reset recovery retains its existing lifecycle.

The Show adapter is server-only and calls this same request contract without a
CLI subprocess. It has no browser endpoint and does not expose private IPC
credentials, caller context, sealed material or grant state to page JavaScript.
Existing Show admission/authentication and avault custody remain unchanged.
Its wheel-shipped module is injected only into the server process. Python
refreshes a per-child, owner-private endpoint snapshot before each admitted
API proxy request, including warm requests. Node rereads it for each operation,
checks the Windows instance response, and never refreshes-and-replays. Failed
verification publishes unavailable; failed publication refuses the dispatch
without restarting or invalidating Show. Retirement fences late refresh workers
from affecting a successor. Recovery is guaranteed at the next independent
proxied API request, not for detached server timers. Existing pages must adopt
the adapter explicitly; this PR does not modify live workspaces. Explicit Node
signal cancellation closes the actual IPC request. The pinned upstream Runtime
does not connect proxy disconnects to handler `request.signal`; that pre-existing
limitation is not changed or counted as end-to-end cancellation evidence here.

Acceptance covers standard/protected and field-ref delivery, approval ownership
and disconnect races, pre/post-handoff one-shot handling, audit metadata, raw
HTTP body/exit status, caller-owned file paths, policy edits during approval,
service-down refusal without SQLite/auto-start, and concurrent context isolation.
Measure before/after in one reserved window with small alternating ABBA batches
(200 calls per side, 20-way), plus the Show pattern of about 30 fetches/minute.
Report CPU-seconds/minute and call latency with load recorded as a covariate;
gate only competing test/build processes, not a load threshold between sides.
No live owner-state writes, service restart, deployment or `vault run` changes
are part of this slice.

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

# Warm CLI control requests: Task, Watch, Session and Run mutations

The remaining Phase 4 mutation slice. It uses only the version 1 contract above
as landed on `master`; it does not depend on the Vault/Show slice (PR #3334)
and shares no helper with it. Performance only: every rule above still holds.

## Operations

`task add|update|pause|resume|remove|run`, `watch add|update|pause|resume|remove`,
`session update|send-now`, `session queue remove` and `runs cancel` become named
operations (`task.add`, ..., `session.queue.remove`, `runs.cancel`). Reads
(`list`, `show`, `get`, `queue list`, `runs list|show`) keep their direct
read-only path. Rollback is the same code rollback of the command family.

## Contract

- Thin client: `main` routes every named operation before invocation authority,
  admission or any store; the process opens no SQLite. Parser usage errors stay
  local. Raw `CALLER_CONTEXT_ENV_NAMES` and the invocation cwd travel as in
  Agent Run; the service resolves authority and applies the same command floor.
- Arguments are checked against the command's own parser leaf: unknown fields,
  wrong types or values, wrong command words, and an omitted required id (every
  final value is checked, defaults included) are refused as
  `invalid_control_request` before admission. Non-finite numbers (`--timeout
  inf`) are carried losslessly so their existing validation still answers.
- `--message-file` keeps its original value, so explicit-flag checks (the task
  action matrix, update mode guards, `message_changed`) are unchanged. The
  client prepares the file when `--message` is absent and the file name is
  non-empty, including stdin and descriptors, and sends the text or read failure.
  The service consumes that result only at the command's original read point,
  so its error keeps its original code and precedence. The service never reads
  a caller path. `task add`'s default timezone is the invocation's, as before.
  Known by design (as for Agent Run): the read itself happens before admission
  and mode checks, so a request the service then refuses may already have
  consumed, or waited for, stdin or a FIFO. Input timing is not a security
  boundary; there is no preflight or second request.
- Every relative or defaulted directory (task and watch `--cwd`, a command
  task's invocation default, a created Session's default workdir) resolves
  against the invocation cwd. An unavailable cwd is refused only where needed.
  A `--cwd` that starts with `~` (Agent Run's included) carries the caller's own
  home, which the service uses for that leading `~` alone; `~user`, the raw value
  in errors, and the existence check where it always was are unchanged.
- Request inputs are explicit: the service passes the caller environment, cwd
  and warm stores into the existing command logic. It never changes process
  environment, cwd, streams or authority outside the request's worker.
- Warm stores: commands use the controller's own Scheduled Task, Watch, Watch
  runtime, execution and Agent stores, refreshing the definition mirrors at the
  start of each request so another process's commit is seen as a fresh CLI
  process saw it. A definition commit publishes the in-process
  `definitions.updated` hint, and the runtime-work lanes reconcile from those
  same mirrors whatever any reader's change probe reported. The one addition is
  a reused public projection reader for the payloads the CLI prints, which no
  controller store provides. Session commands use the cached engine.
- The request body and its arguments (including the parser they are checked
  against) are decoded, and the answer encoded, off the controller loop. The
  answer is encoded with the CLI's own JSON settings for this private Python
  client, so the printed payload is unchanged -- non-finite numbers in error
  details or stored timings included -- rather than normalized by FastAPI.
- The deadline is checked in the worker after executor queueing, before any
  effect. Later phases of a started operation never consult it.
- Watch startup confirmation keeps its success, disappearance, startup-failure
  and unconfirmed outcomes and payloads, but sleeps asynchronously and does every
  read in short threads -- an error's projection included, through the same warm
  reader -- so waiting Watch commands hold no runtime-work worker. The wait keeps
  its whole computed bound, which grows with recovering waiters. Only the service
  can compute it, so `watch add` alone sets no response read deadline; connecting
  stays bounded and every other operation keeps a finite budget.
- `runs cancel` and `session send-now` call the owning `SessionTurnManager`
  directly. The status mapping is the one `/internal/cancel` and
  `/internal/send-now` use, so the existing result classification is unchanged.
  The owner call is shielded from request cancellation and keeps the previous
  30 s transport bound and its `internal_unavailable` classification. Once
  `runs cancel` asks the owner, the command's own durable half (terminalizing
  the Run, or recording the fallback cancel) lands through `land_step` before a
  dropped request's cancellation is answered.
- Reservation rollback, definition write conflicts, removed-definition history,
  command/session task matrices, P0/P1/P3 behavior, Session placement and the
  best-effort Web UI notifications are the owning implementation's. The UI
  notification runs off the executor before the response, as before.
- Output payloads and exit codes are unchanged. Known by design: an exception
  the direct command never caught (a traceback, exit 1) is now the generic
  `task_command_failed` payload, still exit 1, and the service log keeps its
  traceback, naming the operation but not its arguments. Typed errors are
  unchanged.
- A missing or pre-control service is `service_unavailable`; a service with
  control requests but without these operations answers `unsupported_operation`.
  Both are `not_executed`. Post-send uncertainty is `execution_unknown`. No
  fallback write, automatic start, extra request or replay.

## Validation boundaries

1. Existing Task, Watch, Session, Run cancel, harness projection, command-task
   and permission suites run through `in_process_cli_control`: real handler,
   real stores, only the socket replaced, with a test's own stores lent through
   `tests/cli_control_support.py`. Their outcome assertions are unchanged. Live
   owner fakes now answer as the in-process owner (the HTTP adapter's status is
   derived by the shared mapping), and the two remote-Editor creation cases hold
   a real pairing and a reachable target, because a direct `cmd_*` call is now
   admitted exactly as `main` admits it.
2. A full-main test per operation trips on any SQLite open and observes one
   forwarded request; an absent service is not executed and creates no product state.
3. Handler tests cover strict parser parity, deadline expiry after queueing for
   every operation, concurrent caller/cwd/authority isolation on an executor that
   does not copy context, mutations landing in the controller's own mirrors with
   the reconcile hint published, asynchronous Watch startup and direct
   live-owner calls.
4. The Windows control-IPC job runs the new handler suite whenever the request
   contract or handler changes: `main` through the real server, descriptor and
   bearer token for add, pause and remove, and an absent service that creates
   nothing but descriptor discovery's own lock tree.
5. No benchmark, live service, owner state or deployment is part of validation.
