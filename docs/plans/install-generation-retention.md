# Bounded install generations

## Contract and scope

Successful automatic/manual upgrades and installer reruns must not retain one
complete environment per operation forever. Both installer naming schemes use
the shared Python activation lifecycle and the existing `.install.lock`.
No business state, rollback machinery, production installation, or service
restart is part of this change.

The previous GC in PR #1711 was removed because it could not prove ownership:
concurrent staging, alternate launchers, logical interpreter paths, and restart
handoffs invalidated a simple "keep latest two" rule. We therefore collect only
generations with a successful-activation receipt, not arbitrary old directories.

## Retention invariant

- Before switching a launcher, under the install lock, collect receipt-owned
  generations that no recorded stable launcher, live user process, current
  candidate, or source handoff references. The immediately previous target
  remains intact until a later operation.
- Pending, unknown, or unreadable restart ownership, concurrent installer staging,
  and incomplete process/receipt visibility defer collection. Both bootstrap
  scripts publish an installer PID marker before taking their source snapshot;
  another installer's live marker protects its entire handoff.
- Each successful activation attempts to record its stable launcher in its
  generation. All recorded launchers participate in collection, including symlink,
  hardlink, and copy fallback identities. Missing copy markers cannot authorize deletion.
- Failed candidates keep the existing discard path. Cleanup and receipt failures
  are logged and cannot turn a committed activation into failure.
  A failed new ownership receipt leaves that generation permanently unowned and
  excluded from automatic collection; it is not adopted on the next activation.
- With one stable launcher and no extra live references, repeated operations
  retain at most the selected and immediately previous managed generations.
  Extra live processes/launchers retain exactly the generations they need.
  A later successful operation retries deferred cleanup of owned generations
  only. The bound assumes their activation receipts were written successfully.

## Migration limitation (orchestrator approved)

Pre-receipt generations are unowned history, not collectible garbage. Neither
timestamps nor UUID names prove that an unknown custom launcher no longer uses
them. These directories remain untouched, including a legacy source generation.
The previously reported 6.5 GiB on this Mac is **not** automatically reclaimed;
it needs separate operator-verified cleanup. The fixed lifecycle bounds new
accumulation without claiming to infer ownership of released artifacts.
Externally created launcher aliases are outside the managed activation protocol.

## Validation plan

Use test-owned homes and real temporary files/processes. Cover repeated shared
activation, manual/API upgrades, installer reruns, canonical uv environments,
all launcher fallback shapes, logical Python symlinks, pending restarts, concurrent
staging/source handoffs, unowned historical directories, and cleanup failures.
Run focused existing upgrade/installer/restart tests, changed-file Ruff, and the
normal PR CI and exact-head Codex review gates. No production cleanup or restart.

## Local evidence

- Upgrade, installer, retention, and restart suites after the review correction:
  380 passed, including all 38 retention cases.
- First-head retention, dependency repair, and integrity suites: 255 passed;
  the Docker install test is skipped locally because Docker is unavailable.
- First-head CI pipeline contracts: 71 passed. Changed-file Ruff, shell syntax,
  and diff whitespace checks pass after the correction.
- Real temporary Python processes prove logical interpreter/source-argument
  discovery and collection after process exit. A read-only macOS process probe
  exposed `KERN_PROCARGS2` denial for `login`; the existing command-reader
  fallback makes the complete inventory readable without omitting that process.
- Packaged Linux and Windows repeated-install bounds passed in first-head CI;
  every new head still requires fresh exact-head CI and Codex review.

## Review correction

The orchestrator found two gaps on `4bdcfe4d3`: collector restart parsing
accepted unknown future states, and the retry claim obscured permanent retention
after a failed receipt write. Retention now fails closed for unknown/missing/null
restart state without changing shared restart admission. A follow-up activation
test proves a failed-receipt generation stays unowned while subsequent successfully
receipted generations return to the ordinary two-generation bound.
