# Install-generation retention: superseded design and review history

This is historical evidence, not the active contract. The direct user approved
a narrower recovery-oriented guarantee on 2026-09-25 at 01:43:52 Asia/Shanghai.
See `install-generation-retention.md` for the current implementation scope.

## Direct user acceptance revision (2026-09-25 Asia/Shanghai)

The direct user rejected the permanent exclusion of all pre-receipt history:
this PR must reclaim positively identified, unused historical installations as
well as bound future growth. The previous migration limitation below records an
earlier scope decision, not the current acceptance criterion. The implementation
does not yet meet this revised migration criterion; legacy artifact ownership,
concurrent staging and live-reference boundaries require isolated consuming
proof and independent review before publication. Directory age or a timestamp/
UUID name alone will not authorize deletion.

The user also explicitly required no README explanation of this internal
mechanism. The new English section has been removed, restoring both READMEs to
the PR baseline; maintenance documentation and this PR's evidence ledger own the
technical contract. Normal users should not have to manage generations.
Production installation mutation, service restart and merge remain unauthorized.

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
  collection. Relative values in interpreter/script and explicit handoff path
  roles defer collection: the observed cwd cannot prove the launch cwd after a
  `chdir`. Generic application arguments are data, not paths inferred from
  slashes or from a coincidental file in the current cwd. Code/eval operands
  are not interpreted as filenames.
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
  Proven extra live processes/launchers retain the generations they need;
  ambiguous process provenance retains the entire owned set for that operation.
  A later successful operation retries deferred cleanup of owned generations
  only. The bound assumes their activation receipts were written successfully.

## Superseded migration limitation (earlier orchestrator decision)

The original implementation treated pre-receipt generations as unowned history,
not collectible garbage. Neither
timestamps nor UUID names prove that an unknown custom launcher no longer uses
them. These directories remain untouched, including a legacy source generation.
The previously reported 6.5 GiB on this Mac is **not** automatically reclaimed;
it needs separate operator-verified cleanup. The fixed lifecycle bounds new
accumulation without claiming to infer ownership of released artifacts.
Externally created launcher aliases are outside the managed activation protocol.
The process-reference surface is the OS image/cwd, logical argv, supported
interpreter source operands and explicit Avibe handoff options, not arbitrary
code strings, environment variables, open-file/mapping inspection, or the
argument grammar of every third-party application. Unknown supported-interpreter
options and unreadable/ambiguous command fallback still defer collection.

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

## Current-head diagnosis (2026-09-24 UTC)

The direct delivery owner independently exhausted REST reviews, inline comments,
issue comments and reactions, GraphQL threads, and the exact-head Actions job
inventory for `b0c134dd19e3cdd596154b544c480142f6d06171`. Review is terminal
with two findings, not a pass. Eight threads remain unresolved across the PR.
Lint run `36024937039` attempt 1 is terminal failure: Linux installer, Windows
installer and their aggregate fail; the other 15 jobs, including UI, pass.

Six distinct Codex findings-bearing heads are recorded by their review envelopes:
`75f7a7a`, `7a1d186`, `561e4bd`, `bc4c327`, `7c33056`, and `b0c134d`.
The earlier independent findings on `4bdcfe4` are additional evidence, not an
extra Codex review. The repeated process-provenance and launcher-identity classes
require circuit-breaker diagnosis before another edit or push. The owner has
accepted that boundary; a peer independently reproduced Linux's false deferral
and will review the local correction before publication.

| Current evidence | Root cause | Scoped decision |
| --- | --- | --- |
| Shell `-c` text and Git refs with separators defer every collection | Arbitrary argument text was treated as a path without a path-bearing role | Classify interpreter/script and explicit handoff path roles; code strings and ordinary data are not relative path evidence |
| `python worker.py` after `chdir` loses a live source reference | Separator-free script operands were ignored | Recognize source operands even when absent beneath the observed cwd; unknown launch cwd still defers |
| Hardlink lookup returns a directory symlink before the owned real directory | Launcher proof and receipt ownership use different path identities | Normalize the proven generation through the shared canonical path owner |
| Fresh-byte test changed file length | The test did not reproduce the stat-cache collision | Use equal-size bytes and preserve mtime; demonstrate old code failure |
| Windows reaches three generations without a visible deferral reason | Successful installer activation swallows diagnostic output | Expose the existing retention logger on installer stderr and preserve successful activation output in PowerShell |

Windows' specific remaining deferral cause is not yet proven. Do not claim the
shell fix resolves Windows until the packaged Windows consumer demonstrates it.
Keep the receipt schema, shared lock and collection boundary unchanged; do not
add dependencies/workflows or mix unrelated UI changes. The direct user's later
historical-reclamation requirement above supersedes the blanket exclusion of
pre-receipt history, but does not authorize adoption of arbitrary directories.

Independent pre-push review on the unchanged published head found a further
argument-role parsing gap: clustered shell `-eo`/`-euo` options did not consume
their `pipefail` operand. This is the same process-provenance class, not a new
findings-bearing reviewed head. Full REST/GraphQL review and exact-head CI
inventories were independently refreshed; the eight unresolved threads, six
Codex findings-bearing heads and failed run are unchanged. The scoped local
correction must cover clustered shell option operands with real child processes.
Node inline configuration options are self-contained; supported boolean options
must not shift the source position. Unknown option arity and relative preload
paths still require conservative treatment rather than interpreting arbitrary
application data as paths. Final independent review remains required.

Evidence for the revised historical acceptance criterion:

- The immutable initial generation implementation (`2eb8b6dd0`) used
  `generation/tools/avibe-os`, later changed to `generation/uv/tools/avibe-os`.
  Both manual/API runtime staging operations already held the shared install
  lock. Shell installation staged outside that lock and encoded its shell PID
  in the timestamp-based generation name; that is relevant to protecting a
  concurrent legacy handoff, not sufficient ownership evidence for deletion.
- An offline minimal wheel installed by the real local uv 0.9.8 into a private
  temporary home produced `uv-receipt.toml` with an explicit `avibe-os`
  requirement and a `vibe` entrypoint pointing into the exact generation's
  export directory. It also produced `pyvenv.cfg` and the corresponding package
  environment. All HOME/XDG/tool/cache paths were isolated, downloads disabled,
  and the temporary installation was removed by the probe's context manager.
- These existing artifacts are candidate positive ownership evidence, not yet
  an implemented migration or proof of absent external launcher references.
  Independent diagnosis must reconcile legacy aliases and staging before
  historical collection can ship.

Local correction evidence before independent pre-push review:

- Current local validation: 275 retention/upgrade tests and 59 installer tests
  pass (334 total); Docker installer E2E is skipped because Docker is unavailable.
  Changed-file Ruff, compileall, shell syntax and diff whitespace checks pass.
- The new consuming boundary matrix failed 10 cases on unchanged `b0c134d`
  production code; the existing relative-path-with-separator control passed.
  This includes real Bash, sh and Python children, `worker.py` after `chdir`,
  an alias-first hardlink lookup, and ordinary non-path application operands.
- After correction the same matrix passes; the expanded classifier cases cover
  Python option clusters/value operands/`--`, eval and stdin, shell/PowerShell
  source roles and unknown option deferral.
- The subsequent peer-identified cluster/configuration matrix failed 13 cases
  before its correction, with 32 controls passing. The updated focused suites
  passed 352 tests, including actual Bash option clusters; installer diagnostics
  now preserve the exception reason without printing a non-fatal traceback.
- The equal-size/equal-mtime marker regression was executed against the immutable
  `7c33056` marker function in an isolated test process and failed as intended;
  it passes with the fresh-byte implementation.
- The real repeated shell-installer consumer verifies both the two-generation
  bound and visible retention output. The CLI diagnostic test verifies deferred
  collection still returns activation success and restores logging configuration.
- Packaged Windows convergence remains a required remote gate, not a claim
  inferred from the host-platform argument simulation.
