# Retire superseded Avibe installations

## User-approved contract

On 2026-09-25 at 01:43:52 Asia/Shanghai the direct user approved a simpler
recovery-oriented lifecycle. The stable `vibe` command must remain usable;
arbitrary stale processes and commands that directly name retired environments
are not guaranteed to keep working. Their interrupted tasks are not promised
lossless recovery. This supersedes the former universal process/alias retention
contract and permanent exemption for environments without activation receipts.

Normal upgrades and installer reruns must reclaim unused historical Avibe
environments as well as stop future accumulation. Normally one selected
generation remains after handoff. Explicit current invocation, official
service/UI and concurrent handoff references may temporarily require more.
There is no implicit backup/rollback policy.

This is internal installer infrastructure. Both READMEs stay unchanged relative
to the PR baseline. No business-state change, production installation mutation,
service restart or merge is authorized.

## One owner, existing evidence

`vibe/install_generations.py` owns collection under the existing `.install.lock`.
It does not enumerate all user processes, interpret arbitrary interpreter
arguments, maintain an alias registry, or add an activation-receipt schema.

Existing uv installation artifacts identify eligible environments:

- a direct, real child of the managed generation root, never a directory alias;
- either released `tools/<package>` or `uv/tools/<package>` layout for
  `avibe-os` or `vibe-remote`, with `pyvenv.cfg`;
- a readable uv tool receipt naming that package and its `vibe` export into
  this exact generation.

Names and age alone never establish ownership. Unrecognized or incomplete
directories remain untouched. Canonical uv installs outside this root are
outside the deletion scope. Missing Avibe-specific activation receipts do not
exclude a positively identified uv installation.
The generation must contain only its chosen released installation layout and
marker, not a second layout or extra tool. The export `bin/` may contain only
the receipt's exports. Directory aliases are not followed.

## Collection boundaries

After candidate validation and launcher publication, collect retired
environments while preserving:

1. The stable launcher's symlink/hardlink/validated-copy target, including the
   command selected by PATH if this invocation came through a different alias.
2. This invocation's module/interpreter environment.
3. The recorded official service and UI interpreters until their handoff ends.
4. Pending restart and concurrent installation handoffs.

The optional PATH lookup only adds positively identified managed targets to the
keep set. An unrelated command earlier on PATH does not block retirement by a
validated managed launcher; it is outside the deletion scope.

Only official recorded PIDs need process inspection. Unreadable or ambiguous
official runtime references defer collection; unrelated user processes do not.
The collector never signals or terminates runtime processes.

Full restart success and successful normal CLI startup retry retirement after
readiness has been established. Failure to start does not run final collection.
This removes the old official environment once it has actually exited. When no
runtime exists, a validated installer candidate can retire unused predecessors.

Runtime staging and Windows deferred activation reuse the existing installer
PID marker. Ownership is transferred to the helper while the parent still holds
the install lock. Released shell installers encode their PID in the generation
name; this can conservatively protect a live legacy handoff but cannot authorize
deletion by itself. Existing source-snapshot admission remains unchanged.

Cleanup uses a nonblocking acquisition of the same lock and is best-effort.
Busy ownership, unreadable evidence or a filesystem deletion failure keeps data;
no cleanup/marker-removal error after launcher publication can roll back a
successful activation or cause its active target to be discarded.
Retirement removes package contents before uv's receipt and `pyvenv.cfg`.
A partial deletion (including Windows loaded-library denial) therefore keeps
the existing evidence for the next pass; no new tombstone/receipt namespace is
needed. A failure while deleting the final proof files can leave tiny metadata
residue but not a full unidentifiable environment.

## Known by design

- Arbitrary detached workers and explicit paths/secondary aliases into retired
  environments may stop working. The selected stable command remains usable;
  this is recovery, not a guarantee that interrupted work is lossless.
- Canonical uv environments outside `install-generations` are never deleted.
  An outside or unrecognized plain/copy invocation cannot authorize retirement
  of managed generations; collection waits for a later identified activation.
  A different readable PATH-selected command with no managed target adds no
  retention pin and cannot prevent normal managed installations from converging.
- Incomplete, moved, malformed or mixed-use directories without matching uv
  evidence are retained. No directory name, age or missing Avibe receipt makes
  a directory disposable.
- Legacy timestamp shell staging has a liveness-only PID pin. Released Windows
  GUID staging/helpers have no ownership marker and cannot always be
  distinguished from retired installs. They may lose a concurrent candidate
  and need a retry through the selected command. New helpers publish their PID
  under the install lock before reporting a successful handoff.
- An unreadable official runtime reference, pending restart, active installer
  or deletion denial may temporarily retain extra generations. There is no
  universal hard count while one of these references remains unsettled.
- README changes are deliberately removed at the user's request: this is an
  internal lifecycle mechanism, not a new user workflow.

## Verification

All tests use private homes, files and processes. Cover:

- repeated timestamp/UUID installations, manual/API upgrades and canonical uv;
- both legacy layouts and both package names, including a many-generation
  migration fixture without activation receipts;
- active launcher identity across symlink, hardlink, copy, stale marker and
  alias-first directory enumeration;
- current invocation and official service/UI handoff, successful readiness
  versus startup failure, and retired unrecorded old processes;
- pending/future restart records, concurrent staging, legacy shell PID and
  Windows deferred-helper ownership;
- unknown/incomplete/outside/symlinked directories and non-ASCII paths;
- deletion/inspection/marker failures without invalidating successful activation;
- real repeated shell consumption and exact-head Linux/Windows packaged CI.

The initial recovery-contract consumer run against the old implementation was
8 failed / 3 passed: repeated installation across six layout/link combinations,
historical reclamation and unregistered-process retirement failed as expected;
failed-candidate, cleanup-failure and outside-data controls passed. The revised
boundary suite plus readiness consumers passed 59 tests, including real offline
uv installations in private homes; the related six-suite run passed 666 tests
and four subtests. These results precede the pre-push corrections below, and
are not a current-head CI or Codex pass.

### Independent pre-push inventory and scoped correction

The independent review of fingerprint `07230024d8d98fd4aa5ecbe5b4d4f780f36ab4b54e91bbcc98e80071338de21f`
reran 620 consuming tests but reported two actionable classes:

| Class | Evidence | Scope decision |
| --- | --- | --- |
| Positive installation identity | Four mixed-layout/extra-bin fixtures lost unrelated data | Make the permitted root depend on the one chosen layout; allow only receipted exports in `bin/`. Keep evidence-last retirement, independently verified safe. |
| Selected-command authority | Collecting from an outside alias deleted the normal command's target | An outside invocation cannot authorize retirement. Also protect PATH's selected command through one ordinary lookup, covering inside-root aliases of the same class without an alias registry. |

Before the correction, nine regression cases failed (the four mixed-data
shapes, outside/inside aliases through both explicit and inferred callers, and
the canonical-to-managed handoff policy); six normal repeated-install controls
passed. Both findings are accepted. They do not add a reviewed GitHub head.
The root classes remain within the user-approved recovery contract; no global
process scan, new receipt/schema, deletion-scope expansion or user decision is
needed. The fake shell uv fixture must match uv's real export-only `bin/`
layout, with its test support files inside the tool environment.

The minor notes (stale official PID/marker can defer, a future new export is
retained) are conservative liveness limitations, not reasons to delete more.
After this correction the retirement suite passes 64 tests and the real shell
installer suite passes 59 tests. The six-suite run passed 675 tests and four
subtests. Independent re-review of fingerprint
`5f780443dc3ee3d0cf2b4bc73ec8d94153b7ad314e87964e3656281761b5cfac`
confirmed both fixes and passed 867 tests, one existing Windows-only skip and
four subtests, but found one further liveness defect in the optional PATH lookup:

| Class | Evidence | Scope decision |
| --- | --- | --- |
| Optional selector veto | A foreign symlink or plain `vibe` earlier on PATH retained all three managed activations | PATH lookup adds confirmed managed targets only. Remove the unmatched-target veto; retain primary invocation validation and inside-root alias protection. No argv heuristics or extra ownership metadata. |

Both foreign-PATH regression cases fail before this correction, while all ten
normal convergence and inside/outside alias controls pass. This is another local
fingerprint, not a new reviewed GitHub head. The decision restores the approved
historical-reclamation contract instead of accepting unbounded accumulation.
After the correction, all 66 retirement tests pass (17.75s). The expanded
consumer run (retirement, upgrade flow, shell installer, restart supervisor, CLI,
and every runtime suite) passes 869 tests and four subtests, with one existing
Windows-only skip (126.97s; 28 existing SQLAlchemy reflection warnings).
Changed-file Ruff, shell syntax and whitespace checks pass. A terminal
independent review of this complete diff is still required before publication.

Independent pre-push review is required for the complete revised diff and its
failing-before/passing-after matrix. The original Watch remains armed. No manual
Codex review trigger or retry-until-green CI strategy is permitted.

## Review accounting

The published head before this simplification is
`b0c134dd19e3cdd596154b544c480142f6d06171`: eight unresolved threads, terminal
Codex findings, and terminal installer CI failure. Six distinct Codex heads
carried findings before the simplification; earlier independent findings are
recorded separately. Repeated process-provenance and launcher-identity classes
triggered circuit-breaker diagnosis. The direct user's new decision narrows the
guarantees rather than claiming those earlier findings were false.

Historical contracts, findings, controls and test counts are retained in
`install-generation-retention-review-history.md`. Those pass counts do not prove
this revised implementation. Fresh exact-head review/CI, zero unresolved threads
and a direct report are required for delivery; explicit user authorization is
still required for merge.
