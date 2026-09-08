# Model Hub inference deadlines

## Problem and contract

The September 8, 2026 investigation found that Model Hub reused its 60-second
transport budget for response headers, the first response bytes, first model
output, and completion of buffered inference. A connected, healthy provider can
take longer at any of these stages. Retrying discards its work and can exhaust
the caller's retries without ever allowing the model to finish.

An admitted inference request must remain live until protocol completion, a
concrete transport failure, or cancellation by its owner. Elapsed model time
alone is not failure. This property applies to every supported protocol and to
both streaming and buffered responses. Cancellation must still release the
connection, temporary storage, and source ownership, and a waiting request must
not block unrelated requests.

## Scope

- Remove inference wall-clock deadlines in the engine client.
- Retain bounded connection establishment, management/discovery calls, reading
  an already-failed HTTP response, and resource cleanup.
- Start the finite local buffered-response parsing budget after the complete
  response has arrived, so model execution cannot consume it.
- Audit backend idle reclamation, gateway cancellation, and native caller
  timeouts before closing the change. Record evidence and scope decisions here.
- Treat a known source cooldown as delayed admission, with one cancellable wait
  and a fresh resolution at its existing recovery time. Only retry a request
  that had no runnable candidate; never replay visible output or restart a
  failed attempt inside the same gateway request.
- Recognize Responses reasoning-text and compatible Chat reasoning-content
  deltas as model output using the existing protocol taxonomy.
- Keep production configuration and the running service unchanged during
  development; verify with hermetic tests and local Incus where applicable.

## Validation

Exercise delayed inference at each wire boundary beyond the connection budget,
then assert successful completion or owner-driven cancellation. Preserve error
classification, usage, response replay, source isolation, and shutdown tests.

## Audit inventory

| Boundary | Initial evidence | Decision |
| --- | --- | --- |
| Engine response headers / first bytes / first output | Uses transport timeout | Remove inference deadline |
| Buffered inference completion | Uses transport timeout | Remove inference deadline |
| Buffered local parsing | Shares deadline with body download | Give local parsing its own budget |
| Gateway teardown and cancellation drain | Runs after completion/cancellation | Retain cleanup bounds |
| Codex and Claude active-session reclamation | Age backstop overrides durable active ownership | Require the existing ownership veto at both reclamation checks; retain repair of orphaned local flags |
| Codex SSE idle | Native default is 300,000 ms, after HTTP headers | Retain the native policy; zero expires immediately, not disabled. No arbitrary huge override and no early gateway headers |
| Source cooldown | A waiting route returns 409; Codex does not honor HTTP Retry-After | Delay no-candidate admission until the known retry time once; re-resolve current configuration; use 503 and Retry-After for remaining time-recoverable errors |
| Prelude output classification | Responses reasoning text and Chat reasoning content were treated as metadata | Extend the existing taxonomy and acceptance fixtures |
| Prelude storage | Memory spills at 256 KiB; replay has deliberately no total byte ceiling | Preserve lossless replay and cancellation cleanup, not a new response-size rejection policy |
| Codex local control RPC | 120-second acknowledgement deadline; turns run via notifications | Retain, not an inference deadline; resetting initialization on one slow RPC is a separate transport issue |
| OpenCode active turn | Default duration cap already disabled | No change |
| OpenCode admission and stale overlay cleanup | 120-second acceptance check and 30-second old-process drain | Retain; neither limits an accepted live turn |
| HTTP request body | 16 MiB input cap; oversized requests use aiohttp's 413 | Retain size protection; protocol-shaped 413 rendering is a separate issue |

Base inspected: `6bad0ca137ca9a72678511a094d366aa7e48af36`.

## Ownership decision

The ownership snapshot proves an owner still exists, not that its process is
healthy. The old age backstop has no discriminator between a silent valid turn
and a live-but-deadlocked backend. Narrowing the veto to ignore the resource's
own active Turn would therefore restore the same demonstrated false eviction.
This change deliberately prioritizes owner-controlled termination. Explicit
Stop, native terminal/EOF handling, and reclamation of unowned local flags remain.
A live deadlock with no terminal evidence can remain until explicit Stop; do not
claim that this change introduces automatic deadlock detection. Such detection
needs independent backend evidence, not another elapsed-inference threshold.

## Incident evidence

Local service logs and durable Turn rows were compared for the two reported
Sessions. Times below are UTC+08:00 on September 8, 2026.

- `sesssbbj4mt2y`: forced progress-timeout settlement at 00:22:37, 01:21:07,
  09:00:12, and 09:50:27, with native reconnect messages before settlement.
- `sesvmgbdub2gp`: the same forced settlement at 01:31:15. A late native 503
  followed at 01:34:35; both durable rows name the same native Turn.
- `sesssbbj4mt2y` likewise recorded a late 503 at 09:57:16 against the native
  Turn forcibly settled at 09:50:27. These rows are not independent upstream
  outages and must not be counted as such.
- Another observed call cooled its sole source at 09:14:52 until 09:15:22,
  while the caller exhausted reconnects by 09:14:56 and failed with 409.
- The generic 409 at 09:58:14 in `sesvmgbdub2gp` has no attributable gateway
  provenance. It must not be asserted to be the cooldown case without evidence.
  Route/configuration conflicts remain terminal; their safeguards are unchanged.

## Native caller evidence

Verified against the installed Codex 0.153.2 and its exact official source tag:

- [HTTP retry implementation](https://github.com/openai/codex/blob/rust-v0.153.2/codex-rs/codex-client/src/retry.rs)
  uses local backoff and does not read Retry-After.
- [Native regression test](https://github.com/openai/codex/blob/rust-v0.153.2/codex-rs/core/tests/suite/retry_after.rs)
  explicitly asserts local backoff despite a Retry-After header.
- [SSE consumer](https://github.com/openai/codex/blob/rust-v0.153.2/codex-rs/codex-api/src/sse/responses.rs)
  applies its idle deadline to the next parsed SSE event, after headers.
  SSE comment heartbeats do not necessarily reset that event-level deadline.
  The gateway still defers headers until model output or a protocol terminal,
  preserving pre-output fallback and HTTP error semantics.

The native SSE cap and a provider's own limits remain outside the removed
Avibe inference deadlines; this PR does not claim end-to-end infinite patience.

## Validation evidence

- Real loopback HTTP tests cover all three protocols, five delayed inference
  boundaries, and completion/cancellation. They assert no timeout or cooldown
  classification for slow successful inference and closed client resources.
- Gateway HTTP tests cover known cooldown, repeated failure, configuration
  changes during the wait, and cancellation for every backend.
- Existing protocol classification, byte replay, usage, teardown, native
  termination, and stale-local-flag repair suites remain required.
- Focused combined local validation: 1,223 passed, 30 subtests passed, one
  previously expected xfail. Changed Python files pass Ruff.
- Production configuration, credentials, and installed service are unchanged.
