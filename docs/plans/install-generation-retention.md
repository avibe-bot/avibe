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
- Process references are collected for the managed install owner's filesystem
  identity, the updater itself, and recorded service/UI PIDs. Owner, command
  line, executable, or cwd inspection that is denied or ambiguous defers
  collection. Relative argv paths are resolved against the process cwd for
  positive evidence, but a current cwd cannot prove the launch cwd after a
  `chdir`, so collection retains all owned history for that operation.
- Each successful activation attempts to record its stable launcher in its
  generation. All recorded launchers participate in collection, including symlink,
  hardlink, and copy fallback identities. Once a symlink, hardlink, or valid copy
  marker proves one generation, collection does not broaden that proof into a
  byte match across identical wheel copies. Missing or stale copy markers use the
  conservative byte fallback and cannot authorize deletion without a match.
- When a second launcher targets an already receipted generation, its launcher
  reference is written under the install lock before the launcher is published.
  A failed reservation leaves the launcher and existing receipt unchanged. Fresh
  candidates are still receipted only after activation.
- An alias activation targeting an unreceipted generation never creates a receipt;
  this preserves the permanent-unowned migration and failed-receipt contract.
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

- The focused retention suite after the safety-boundary correction: 60 passed.
- The install/upgrade consumer suite (`test_upgrade_flow`, retention, installer
  script, and install-command E2E): 292 passed, 1 skipped locally because Docker
  is unavailable.
- First-head retention, dependency repair, and integrity suites: 255 passed;
  the Docker install test is skipped locally because Docker is unavailable.
- First-head CI pipeline contracts: 71 passed. Changed-file Ruff, shell syntax,
  and diff whitespace checks pass after the correction.
- Real temporary Python processes prove logical interpreter/source-argument
  discovery and collection after process exit. A read-only macOS process probe
  exposed `KERN_PROCARGS2` denial for `login`; command-reader fallback handles
  recoverable argv access, while denied owner/cwd/cmdline/exe provenance defers
  collection instead of guessing.
- Packaged Linux and Windows repeated-install bounds passed in first-head CI;
  every new head still requires fresh exact-head CI and Codex review.

## Review correction

The orchestrator found two gaps on `4bdcfe4d3`: collector restart parsing
accepted unknown future states, and the retry claim obscured permanent retention
after a failed receipt write. Retention now fails closed for unknown/missing/null
restart state without changing shared restart admission. A follow-up activation
test proves a failed-receipt generation stays unowned while subsequent successfully
receipted generations return to the ordinary two-generation bound.

## Safety-boundary correction

The review of `7a1d186dc5` identified three manifestations of two root causes:

| Finding | Root cause | Contract-preserving boundary |
| --- | --- | --- |
| A different-user worker was skipped | Process visibility was scoped to the updater's username | Match the managed root owner through platform/filesystem identity, while always including the updater and recorded service/UI PIDs; uncertainty defers collection |
| A relative interpreter path was skipped | Logical argv was filtered before cwd/provenance handling | Normalize quoted fallback argv, resolve relative values against the observed cwd, and defer when the launch cwd cannot be proven |
| An existing receipt lost a newly published alias | Receipt ownership was updated after launcher publication | Reserve the alias in the existing receipt under the same lock before publication; failed reservation aborts without changing the launcher |

The exact-head CI UI failure is a separate test-harness issue: all 6,023 UI
assertions passed, then a delayed `ToastProvider` timer accessed `window` after
test teardown. It is intentionally outside this retention patch.

The current process-scan correction skips only Linux tasks positively marked
`Kthread: 1` in `/proc/<pid>/status`. Empty or unreadable command lines from
ordinary userspace processes still defer collection; no process is ignored
based only on a name, status, or missing argv.

The local pre-push review also required narrow hardening corrections:
bare interpreter names from other processes and separator-free source arguments
fail closed as relative references; only the current collector's own bare
interpreter token is ignored. Ambiguous unquoted fallback command lines defer
collection, including path-bearing options whose quoting cannot be proven.
Inline `--candidate=`, `--launcher=`, and `--source-generation=` operands are
also inspected rather than discarded as flags. Windows owner probes declare
pointer-sized API handles and skip PID 0. A protected Windows process may use
the platform-reported account name when the token handle is unavailable; the
managed root carries the corresponding SID/account identity, while an
unreadable or ambiguous identity still defers collection. Processes that exit
during owner inspection are skipped as a normal snapshot race rather than
deferring the whole collection pass. An alias cannot adopt an unreceipted
generation. Copy fallback byte comparison is used only when the launcher has
no proven symlink, hardlink, or valid generation marker; this keeps repeated
identical wheel installs bounded while retaining marker failures conservatively.
