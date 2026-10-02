# Recovery invariants (C-5, C-7)

These are properties, not mechanisms. Each invariant names the lane that owns it and the test that proves it; the
mechanism lives in that lane's code, where the test can exercise every crash point. A contract change is needed only
when a mechanism needs a field another lane reads; those fields are listed in the last section.

General rule: every external effect records its intent durably before the effect and its outcome durably after it,
and recovery reconciles intents that have no outcome. An effect on a system without an idempotency key is
at-least-once, and that is stated where it applies.

## Jobs (`tools` lane; Watch `job` target in the adapter wave)

| ID | Invariant | Proof |
| --- | --- | --- |
| J1 | A command starts at most once, and recovery can always decide whether it may have started. A missing or late file never proves that it did not. | crash injected at every step of the launch, then recovery; the command's side effect (a counter file) is at most 1, and recovery's verdict matches it |
| J2 | Before a command can run, the job record holds an identity that distinguishes its process tree from a recycled pid, so recovery can inspect and kill exactly that tree. | kill and inspect after a simulated pid reuse |
| J3 | A `timeout` holds across handover and vibe restarts: the deadline is absolute and recorded with the job, and whichever component owns the job when it passes kills the tree. | timeout shorter than the foreground window with `watch: true`, and with a restart in between |
| J4 | Output on disk is bounded per job (head and tail kept, middle dropped beyond a fixed cap), and every result or follow-up built from a truncated log says so. | a command producing more than the cap |
| J5 | Job files are kept until the owning tool call has a durable `tool_result` and the Watch that owns the job, if any, has settled. | an exited job whose call is unsettled is never removed |
| J6 | Handover is idempotent per job: one job has at most one Watch, whatever crashes in between. | crash between Watch creation and committing the handover result, then recovery |

## Tool calls (`loop` lane)

| ID | Invariant | Proof |
| --- | --- | --- |
| T1 | `project()` reads only committed rows; the same rows always produce the same request. | projection before and after job state changes |
| T2 | At resume, before the first projection, every tool call without a committed `tool_result` gets exactly one, chosen from its job state (exited: the output; running: handover; never ran or unknown: the interrupted result). Retries settle nothing twice. | crash between settlement steps, then resume twice |
| T3 | Inputs accepted by `steer` or `follow_up` are never dropped: they enter the context or are returned to the adapter for the P3 queue. | a terminating tool while a steer is queued |

## Delivery (adapter wave)

| ID | Invariant | Proof |
| --- | --- | --- |
| D1 | A committed response is delivered completely: every part a surface splits it into is either confirmed or retried. A partial delivery is never recorded as delivered. | failure on a later part, and a crash between parts |
| D2 | Workbench delivery is exactly once (the row is the message). IM delivery is at least once per part: `BaseIMClient.send_message` has no idempotency key, so a crash after the platform accepted a part but before its receipt was committed resends that part. | crash after accept, before receipt |

Today `core/message_dispatcher.py` swallows failures of later split chunks and returns the first chunk's id. The
adapter wave must not reuse that path as is for agent rows; D1 is the requirement it meets.

## Shared fields

The only cross-lane shapes recovery adds:

- `meta.json` (`job.schema.json`): `deadline_at` (absolute, nullable), and `process` with the identity fields of
  `PersistedProcessIdentity` (`core/process_isolation.py`), recorded before the command can run (J2).
- Watch `job` target: keyed by `job_id`, created by adopt-or-create (J6).
- Response rows: `metadata_json.delivery` with one entry per part (`transcript.md` §2).
