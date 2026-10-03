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
| T2 | At resume, before the first projection, every tool call without a committed `tool_result` gets exactly one, chosen from its job state (exited: the output; running: handover; never ran: the interrupted result). A call without job state (`write`, `edit`) gets `[tool call interrupted; it may or may not have completed; re-read the file before continuing]`. Retries settle nothing twice, and neither do concurrent writers: the store's `append_tool_result` returns the result already committed for the call instance instead of writing a second. | crash between settlement steps, then resume twice |
| T3 | Inputs accepted by `steer` or `follow_up` are never dropped: they enter the context, including after a crash. They belong to the Turn that accepted them and are never re-queued as a new P3 Turn. A run that ended by design runs again for them within its Turn; after a stop, an error, or a crash they are admitted into the context (at the end of the run, or at the Session's resume) and share the Turn's outcome. | a terminating tool while a steer is queued; a crash before consumption |
| T4 | An unsettled Turn found at startup is settled as interrupted after T2; the agent never continues it on its own (a hook `end`, an abort, or a crash all leave the same safe state). The user's next message starts a new Turn with the full context. | crash after an `end` hook and after a mid-turn commit |

## Delivery (adapter wave)

D1 and D2 are withdrawn (owner decision, 2026-10-03, PR #2345). The Avibe Agent delivers each committed response
through the same emit path as the other backends: splitting, file upload, failure handling, and narration settings
are the dispatcher's. The one difference is persistence: the response row already exists, so the dispatcher writes
its display columns instead of inserting a second row (`transcript.md` §2). A crash or send failure between commit
and send loses that delivery, as it does for the other backends; the transcript, and so the context, is unaffected.
A Turn interrupted after its final commit is settled by T4 like any other, so the user sees the interruption
notice, and the committed reply stays in the Workbench transcript with its commit-time display.

## Shared fields

The only cross-lane shapes recovery adds:

- `meta.json` (`job.schema.json`): `deadline_at` (absolute, nullable), and `process` with the identity fields of
  `PersistedProcessIdentity` (`core/process_isolation.py`), recorded before the command can run (J2).
- Watch `job` target: keyed by `job_id`, created by adopt-or-create (J6). The hand-over takes the Watch's Agent and
  authority from the job's owning Turn, never from the caller of the hand-over, and records that authority
  explicitly: the Turn's remote snapshot, a local marker, or an unverifiable snapshot (a remote Turn whose
  authorization cannot be found). A job Watch without a verifiable authority still owns its job but never follows up.
