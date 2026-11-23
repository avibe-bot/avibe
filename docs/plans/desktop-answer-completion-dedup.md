# Keep the answerable notification for one turn

## Contract

On macOS, a successfully submitted answerable Agent notification replaces the
ordinary success notification for that exact turn. It does not replace failure,
cancellation, Vault, unrelated turns, or independent command-task notifications.
An answerable candidate that was filtered, refused, or failed to submit cannot
suppress a success notification. Existing reply actions and their authority stay
unchanged.

The owner authorized a local candidate only on 2026-11-23: no push, PR, release,
installation, service restart, or real notification test. Main coordinates
acceptance in the next Desktop package.

## Evidence and identity

The rc14 reference is `2240ae27b2`; implementation starts at
`70593397399c1a7cce389abc1cada42c8c7f0977`.

On 2026-11-23 at 08:01:35 +08, the main Bot's morning-brief result produced
an answerable UN notification followed by a plain run-success notification.
The message's `metadata.turn_id` and the successful run's `metadata.turn_id`
are identical. Their `metadata.run_id` / run ID also agree. The OS log records
both requests for that session, with the answer category on only the first.
The times identify the diagnostic sample; they are not correlation keys.
The sample predates the rc14 installation (it ran on rc13); the relevant
independent question/success paths are also present in the rc14 reference.
Message `msg_00660e64c1409fc973ae2e4` and run `babad79f7ad7` both name
turn `trn_a451acfeda1041d0a5fc7ccd35e43a8b` in Session `ses_main`.

`message.new` already carries public `metadata.turn_id`. `runs.updated` is a
refetch hint; the existing `/api/harness/runs/{id}` read returns the run's
`session_id` and `metadata.turn_id` (confirmed against the installed Runtime).
No new server field, endpoint, or schema is required. Missing or inconsistent
identity preserves the existing success notification.

Correlation is scoped to the adopted Runtime connection, presentation epoch,
session ID, and turn ID. Reconnecting the same stream may retain evidence;
changing the Runtime connection or presentation cannot reuse it.

## Implementation and validation plan

- Register an answer attempt before asynchronous detail lookup so a concurrent
  success lookup cannot overtake it.
- Reuse notification receipts to learn whether the OS actually accepted a post.
  Only the native sink that supports reply actions opts into suppression.
- A matching success waits for already observed attempts to settle without
  holding detail-read slots or preference locks. Failure leaves the original
  success path available; successful submission suppresses only that turn.
- Bound a missing native callback to 30 seconds, then keep the completion.
  Recheck the exact turn again at the native submission boundary, so a late
  successful question can still suppress a queued completion. If the completion
  has already been submitted before a late question succeeds, both may exist;
  unknown OS outcomes favor delivering attention over silently losing it.
- Bound retained attempt evidence, and preserve ordinary behavior when evidence
  is absent. Do not infer an unseen question from a nearby run.
- Add an old-red/new-green regression through SSE parsing, detail lookup, and
  notification delivery. Cover interleaved turns/sessions, independent tasks,
  failures, missing revisions/identity, filtered/refused posts, asynchronous OS
  completion, reconnects, and presentation changes.
- Run focused Rust checks and review the exact local candidate before handoff.
  Record the candidate commit and remaining real-package acceptance below.

## Acceptance (requires a separately agreed visible test slot)

1. With the local Workbench unfocused, finish a background turn with quick
   replies. Its answerable notification remains; no ordinary success follows.
2. Verify a shortcut and typed reply still enter the correct conversation.
3. Finish a different turn and an independent background task: their ordinary
   completion notifications remain.
4. Confirm a failed run still reports failure.

No real notifications have been sent for this implementation.

## Local validation

- The delivery-path regression
  `a_posted_answer_replaces_only_its_turns_success_notification` failed against
  the unchanged implementation with `[AnswerRequested, RunSucceeded]` instead
  of `[AnswerRequested]`, then passed after the fix.
- 46 notification integration tests, 8 real loopback HTTP contract tests, and
  16 notification unit tests passed.
- Runtime-host Clippy (library and both integration targets, warnings denied)
  and macOS `avibe-desktop` library compilation passed offline with the already
  installed Rust 1.97.1 toolchain. The default Rust 1.90 cannot compile the
  base branch's existing iroh dependencies; no dependencies were changed.
- The native OS callback is simulated by the integration sink. Actual
  notification presentation and reply interaction remain package acceptance,
  not a claimed result of these tests.
- Overload or unavailable run detail preserves existing completion delivery
  when it qualifies without that read. Without exact turn evidence, this may
  retain a duplicate; it never guesses which completion to suppress.

The local candidate is on `fix/desktop-answer-completion-dedup`. Its commit and
independent review outcome are supplied with the handoff; no push or PR is
authorized.
