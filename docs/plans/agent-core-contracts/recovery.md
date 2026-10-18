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
| J5 | Job files are kept until the call instance that started the job has a durable `tool_result` and the Watch that owns the job, if any, has settled. An earlier call's result never settles a later call that reuses its id. | an exited job whose call is unsettled is never removed, including after a reused id and a wall clock that stepped back |
| J6 | Handover is idempotent per job: one job has at most one Watch, whatever crashes in between. | crash between Watch creation and committing the handover result, then recovery |

## Tool calls (`loop` lane)

| ID | Invariant | Proof |
| --- | --- | --- |
| T1 | `project()` reads only committed rows; the same rows always produce the same request. | projection before and after job state changes |
| T2 | At resume, before the first projection, and when a run ends without completing (a Stop), every tool call without a committed `tool_result` gets exactly one, chosen from the state of the job its call instance started (exited: the output; running: handover; stopped with a recorded reason: the output so far and that reason, `tools.md` §5; never ran: the interrupted result). The resume of a Turn continued after a restart (T4) first waits for a running job as its live call would: until it exits, until the call's foreground window passes, counted from the job's launch (`meta.created_at`), or until a Stop, which ends the command as in a live Turn; only a job still running then is handed over, and one no Watch can take stays in the foreground until it ends, as in a live Turn. Startup's own pass leaves a running job of a Turn still unsettled to that Turn's owner, which continues the Turn or settles this Session itself before its notice. The wait commits nothing and starts nothing, so a crash during it leaves the call open for the next settlement. A call without job state (`write`, `edit`) gets `[tool call interrupted; it may or may not have completed; re-read the file before continuing]`. Retries settle nothing twice, and neither do concurrent writers: the store's `append_tool_result` returns the result already committed for the call instance instead of writing a second. | crash between settlement steps, then resume twice; a Stop while several calls of one group (C-3 §2) run; a restart continuation whose job exits inside its window, outlives it, or meets a Stop |
| T3 | Inputs accepted by `steer` or `follow_up` are never dropped: they enter the context, including after a crash. They belong to the Turn that accepted them and are never re-queued as a new P3 Turn. A run that ended by design runs again for them within its Turn; after a stop, an error, or a crash they are admitted into the context (at the end of the run, or at the Session's resume) and share the Turn's outcome. | a terminating tool while a steer is queued; a crash before consumption |
| T4 | An accepted Turn found unsettled at startup is continued after T2, whatever stopped the process (`vibey-restart-auto-resume.md`): a Turn whose final response was committed settles from it, whatever arrived after it (a late Stop loses the race, as live), and a silent final leaves the Turn's last visible text as its result, as live; any other Turn hands its work, and its Runs (including those of steers it accepted), to a hidden continuation that resumes the committed transcript ahead of input queued behind it, with no notice and no replayed tool call. A command still running is waited for by the continuation's own T2 within its window from launch and is never run again; one handed to a Watch is read as the handover result. A Turn that no longer qualifies (Stop or replacement requested, Session archived, Agent off Vibey, or the third consecutive continuation without a model response) settles as interrupted with the restart notice after T2 hands its running commands to Watches (it stays open, retried, until a Watch takes them), so that notice names them and offers no Retry, because a resend would run them twice. A run that fails while the service is stopping leaves its Turn open for this. | crash after an `end` hook and after a mid-turn commit; a restart during a tool call, during a model call, and after the final commit |

## Delivery (adapter wave)

D1 and D2 are withdrawn (owner decision, 2026-10-03, PR #2345). Vibey delivers each committed response
through the same emit path as the other backends: splitting, file upload, failure handling, and narration settings
are the dispatcher's. The one difference is persistence: the response row already exists, so the dispatcher writes
its display columns instead of inserting a second row (`transcript.md` §2). A crash or send failure between commit
and send loses that delivery, as it does for the other backends; the transcript, and so the context, is unaffected.
A Turn interrupted after its final commit is settled by T4 from that reply, without a notice, and the committed
reply stays in the Workbench transcript with its commit-time display.

## Shared fields

The only cross-lane shapes recovery adds:

- `meta.json` (`job.schema.json`): `deadline_at` (absolute, nullable), and `process` with the identity fields of
  `PersistedProcessIdentity` (`core/process_isolation.py`), recorded before the command can run (J2). `session_id`,
  `response_id`, `response_seq`, and `tool_call_id` are the call instance that started the job. T2's job lookup,
  J5's settled check, and the hand-over's owning Turn all match on it, in context order; none compares a job's
  `created_at` with a row's time.
- `tool_result` `details.recovered`: `true` on every result T2 commits, so T4 tells the Watches recovery created for
  the Turn's running commands from those the Turn handed over itself (`storage.agent_transcript.recovered_watch_ids`).
- Watch `job` target: keyed by `job_id`, created by adopt-or-create (J6). The hand-over takes the Watch's Agent and
  authority from the job's owning Turn, never from the caller of the hand-over, and records that authority
  explicitly: the Turn's remote snapshot, a local marker, or an unverifiable snapshot (a remote Turn whose
  authorization cannot be found). A job Watch without a verifiable authority still owns its job but never follows up.
