# Watch Follow-up Serialization and Circuit Breaker

## Background

Issue #953 recorded a level-triggered `forever` Watch whose waiter kept exiting
successfully while the condition stayed true. The supervisor treated every exit `0`
as a new event and immediately re-ran the waiter. In 18.139 seconds it created 274
Agent Runs: 174 completed no-op Runs and 100 more queued Runs.

The failure is not primarily a queue-capacity problem. A Watch event is unfinished
until its Agent follow-up has settled, and a level that remains true is not a new
event. The runtime currently loses both facts between cycles.

## User Contract

- `once` means wait until the first reportable event. An explicitly configured retry
  exit code, including the default `75`, re-runs the waiter after `--retry-delay`
  instead of retiring the Watch.
- `forever` means monitor a sequence of distinct reportable events. Exit `0` means
  one new event. A waiter that is still waiting returns an allowed retry exit code;
  a completed but uninteresting cycle may use exit `64` plus
  `avibe-watch: no-event`.
- Other non-zero exits are terminal failures. Exit `124` is retryable only when it is
  explicitly listed.
- `--lifetime-timeout` bounds one armed episode in either mode. Creation or explicit
  resume starts that episode; supervisor and service restarts retain its deadline.
- The lifetime deadline also bounds time spent behind the follow-up fence. If an
  earlier Agent Run is still queued or running at the deadline, the Watch retires
  immediately and records that Run's actual ID, but does not create a second
  concurrent Run. If no Run owns the slot, the timeout follow-up uses the configured
  language.

## Runtime Invariants

1. A Watch definition owns at most one queued or running Watch follow-up Run.
2. A `forever` Watch does not start its next waiter until the prior follow-up Run is
   terminal, then waits at least five seconds before re-arming.
3. The accepted event timestamp and follow-up Run ID are stored on the Watch in the
   same transaction that queues the Run.
4. At most five successful events are accepted in a rolling 60-second window. The
   sixth successful waiter output is diagnostic evidence, not a normal event.
5. The sixth success atomically pauses the Watch and queues one circuit-repair Run to
   the same Session. A restart can observe that durable decision and cannot queue a
   second repair Run.
6. Resume clears the live burst window but retains the last follow-up Run ID. If the
   repair Agent resumes the Watch before its own Run settles, the waiter still waits
   for that Run and the five-second re-arm delay.
7. An allowed retry exit is healthy while the Watch remains enabled, regardless of
   mode. The same exit is failing once the Watch retires because no retry remains.

The SQLite outbox transaction also rejects a new Watch follow-up while an older one
for the same definition is queued or running. The runtime-level wait handles normal
flow and legacy rows; the transaction-level check closes admission races.

## Circuit Repair Message

The repair Run receives the Watch ID and name, the six observed timestamps, exit
code, bounded stdout/stderr, the Watch exit-code contract, and commands to inspect,
update, and resume the Watch. It must inspect and fix the waiter so persistent levels
return retry/no-event rather than success. It may resume only after an unambiguous,
reversible fix is verified; otherwise it leaves the Watch paused and reports why.

## Persistence and Recovery

No schema migration is required. Watch metadata owns:

- the latest follow-up Run ID, used for terminal-state and re-arm checks;
- the rolling accepted-event timestamps;
- the last circuit-breaker incident and repair Run ID;
- the current armed episode's lifetime origin.

Existing Watches without a lifetime-origin key derive it from their durable
`created_at`; the next explicit resume writes a new origin. On upgrade, the runtime
also queries queued/running Watch Runs by definition before starting a waiter, so
legacy backlog drains without admitting another event. That queried row, rather than
possibly absent or stale Watch metadata, is also the source of the blocking Run ID
recorded on lifetime expiry. File-backed stores retain the same behavior for tests and
legacy operation, without pretending to provide a cross-file transaction.

Changing the waiter command, shell command, mode, or cwd starts a new waiter lifecycle
and clears the old burst window, but does not reset the armed episode's lifetime.
Session routing and follow-up copy changes do not reset either one.

## Observability

The circuit decision leaves the definition disabled with no retirement timestamp, so
it reads as paused after the repair Run settles. `last_error` states the threshold and
the repair Run ID; the incident metadata preserves the six timestamps. The repair Run
uses a distinct Watch outcome value so failure notices do not misclassify it as an
ordinary event or waiter failure.

## Verification

- `once`: retry, timeout-as-retry, first success, terminal failure, and lifetime limit.
- `forever`: queued/running single-flight, terminal re-arm delay, quiet and retry
  cycles, legitimate edge-triggered recurring monitoring, and terminal failures.
- Circuit: five accepted events, sixth pause, no sixth ordinary Run, one repair Run,
  bounded evidence, resume behavior, and restart idempotence.
- Storage: atomic queued-run admission rejects an existing unfinished Watch follow-up.
- Contracts: CLI help, Agent system prompt, background-watch skill, and scenario
  catalog describe the same exit-code and repair semantics.

## Delivery Risk

The main risk is reducing observation cadence for waiters that relied on re-arming
while their Agent was still processing. That behavior is the source of the incident;
the supported replacement is a durable cursor or state transition in the waiter. A
five-second post-Run delay is intentionally fixed rather than user-configurable: it is
a runtime safety floor, not domain polling policy.
