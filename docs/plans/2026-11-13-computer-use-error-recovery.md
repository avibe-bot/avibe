# Computer Use terminal error recovery

## Problem and approved contract

An enabled Computer Use record with `error/health_timeout` survived app
relaunches without another driver start. Restore retained Error and both
Runtime capability handling and native ticks skipped that phase.

Recover only `health_timeout`, `daemon_exited`, `socket_unreachable`,
`spawn_failed`, and `endpoint_busy`. Other reasons, including the heterogeneous
`driver_health_failed` fallback, keep their existing remediation.

There are at most three actual automatic recovery starts per shell process:
one immediate restored-error attempt after Runtime and permission prerequisites
pass, then two attempts with minimum 30-second and 120-second backoffs.
Subsequent attempts require the main window to be visible and not minimized.
The existing activation event and five-second tick observe visibility; opening
Settings in an active window is covered within one tick.

An eligible terminal failure reached in the current process waits at least
30 seconds before its first recovery start and shares that process budget.
Success and explicit toggles do not refill the automatic budget. Explicit
off/on retains its ordinary start authority and cancels pending recovery.
Recovery starts never multiply into the existing fast retry cycle.

The lifecycle owns admission, deadlines, and counting. Runtime/permission
transitions cannot bypass admission. Recovery uses Starting with the retained
failure reason; success publishes Ready with no residual error. Exhaustion
publishes Error with the final reason and existing localized next-step guidance.
The persistent schema and health protocol/deadline are unchanged.

Package validation fails closed; state-write failure stops startup. Permission
checks remain silent during recovery. Setup-resume markers retain their
existing arming, settlement, and single-launch consumption rules. Tests never
touch the installed shell or running Avibe.

## Verification ownership

The previous tests covered fast failure limits and restored prerequisites,
but not recovery of terminal errors. New lifecycle tests protect the actual
start bound, elapsed-time/visibility gates, prerequisite reentry, explicit Off,
and Ready clearing the error without refilling the budget. Each would catch a
credible reset, duplicate admission, or bypass of those transitions.

The native adapter harness covers a different boundary: real disposable
fake-driver processes, a fixture capability server, and test-owned state,
lock, socket and log paths exercised through production restore and startup
helpers. It verifies process starts and persisted results rather than mocking
the lifecycle decisions. Existing setup-resume tests protect its unchanged
marker semantics.

UI and native label tests protect the new restarting copy, actionable terminal
copy, and the absence of old error text after Ready. Both locale catalogs
remain aligned.

Local validation uses focused Rust and UI tests, desktop localization checks,
format/lint checks and desktop/UI builds. The English PR must have an exact-head
Codex pass, zero unresolved review threads, all expected CI green, and a clean
merge state. The lane keeps one combined durable PR Watch. Merge and release
remain outside this assignment; installed signed-app/TCC acceptance belongs to
the subsequent integration release.
