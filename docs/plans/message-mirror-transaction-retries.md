# Message mirror transaction retry slice

## Contract and boundary

- Every mirror retries one complete scope/message (or inbound link) transaction with
  `storage.db.retry_sqlite_write`; scope resolution and nested media registration
  must let database failures reach that owner.
- A retry starts from frozen caller inputs. Rolled-back media URLs, citation spans,
  and provenance must never become the next attempt's source payload.
- Async callers land only persistence on a worker. Publication, error sinks, caller
  receipt state, and live context transitions stay on the calling loop. A cancelled
  caller owns its completed receipt before cancellation resumes at its next await.
- Publication and transport are not retried. Publication failure cannot invalidate
  an already committed receipt. Synchronous APIs remain for actual worker callers.
- Exhaustion is logged and returned to existing receipt recovery. Legacy inbound
  and backfill have no durable replay owner: exhaustion can leave incomplete
  transcript history, but must not suppress a live inbound turn. This slice does
  not invent an inbound journal or replay transport.
- Show keeps synchronous loop-owned lifecycle hooks. Only scalar inbound linking
  work goes on its existing ordered worker ahead of checkpoint reads. Turn-start
  wait reports failure without breaking best-effort legacy dispatch; terminal-only
  link failures are logged on worker completion.

## Ownership

One eventual integration PR is owned by Session `sespzhunh64y2`. This slice does not
push, open a PR, or merge. Claude caller replacements are supplied to that owner;
`modules/agents/claude_agent.py` and `core/scheduled_tasks.py` are not edited here.

## Validation contracts

Existing mirror tests validate successful writes, projections, and duplicate IDs,
but not a rolled-back complete transaction or loop/thread ownership. New regression
cases therefore exercise:

1. Scope and message rollback together, including failure at either write boundary
   across agent, harness, human, and silent-terminal writers.
2. A real independent SQLite writer held beyond the five-second busy timeout;
   retries succeed while a heartbeat advances and raw bus publications occur once,
   on the caller loop.
3. Media/citation reconstruction from original input after rollback; nested media
   database errors must not degrade into unregistered links.
4. Exhaustion returns failure; publication failure preserves the committed receipt;
   cancellation lands the receipt and publishes before returning cancellation.
5. Dispatcher transport is sent once across persistence exhaustion/recovery, using
   real mirror transactions rather than mocks implementing mirror behavior.
6. Show link completion precedes checkpoint reads, without moving context/bus
   ownership off-loop, and exhausted linking is observable through its waiter.
   Stop must land an in-progress link but must not wait for a blocked git job.
   Earlier path-owned lifecycle starts read their durable Delivery snapshot,
   independently of legacy inbound backfill.

Tests use the repository's pre-import HOME/XDG/AVIBE_HOME isolation in conftest.
No service changes, live runtime probes, or remote regression are authorized.

## Local evidence

- Focused integration run: 835 passed, 164 subtests passed (mirror, dispatcher,
  handler, auth, Show, media, link delivery, Session CLI, local-error projection).
- Shared backend/Show worker consumers: 115 passed (`test_agent_service.py`,
  `test_bot_git.py`).
- Replaying the pre-fix `a8e5c67b5` mirror module against the new synchronous
  rollback/rebuild regressions: nine fail for missing second transaction/media
  reconstruction, not for an unrelated API change.
- Changed Python files pass Ruff; no service/runtime operations were performed.
- Read-only review reported no findings and independently passed 474 tests plus
  152 subtests. Its remaining Show cancellation test gap was filled with two
  parameterized cases; the final mirror/Show-focused rerun passed 26 tests.
