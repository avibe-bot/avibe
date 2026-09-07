# Authority Scanner Source Boundary

## Scope and Decision

The final CI quality item changes only the Model Hub authority scanner and its
contracts. PR #1921 has already merged and its exact-source master CI passed.
No application behavior, authority policy, workflow, dependency, test selection,
timeout, runner count, or shard weights change. Dedicated CI optimization stops
after this item and its merged-source gates.

The old recursive scans include ignored virtual environments, build outputs,
and nested worktrees. A bounded local profile read 15,795 distinct files and
parsed 14,493 Python paths, including 13,637 under a private virtual environment.
Only nine parses repeated a filename: source scope, not AST caching alone, is
the main defect. The original two CI authority checks each took about 10 seconds
on both the #1921 PR and master. Local profiled durations are not CI speedups.

## Contracts

- Repository discovery uses Git's tracked plus untracked, non-ignored files.
  Tracked files remain in scope even when an ignore rule matches them. New
  legitimate source files do not need staging or a known top-level directory.
- Importer discovery and all registry glob scans use the same boundary and Git
  glob semantics, including root files matched by a leading `**/`.
- This repository development checker requires Git and an exact checkout root,
  including linked worktrees. Enumeration failure cannot produce a passing
  verdict. Explicit registered inputs remain required regardless of ignore rules.
  Source reads may not escape the root through parent paths or symlinks.
- File bytes and selected consumer ASTs are reused only within one check's
  `AuthorityInput`. Bulk importer trees are not retained. A new check gets new
  source discovery, bytes, ASTs, findings, and a content fingerprint.
- JSON is decoded anew from invocation-local bytes; callers and the existing
  persisted-version mutation probe cannot share a mutable cached JSON object.
- Live-file checks, ownership findings, all normative absence/version checks,
  and the existing `AuthorityInput.json` mutation contract remain effective.

Git excludes ignored untracked dependencies and does not recursively inspect
submodules as if their implementation belonged to this repository. This is an
explicit source-ownership boundary, not a security sandbox or an atomic snapshot
of concurrent filesystem edits. Files observed during one invocation keep their
first-read bytes; a later invocation observes changes again.

## Validation

- Eleven new boundary/cache cases and the unchanged Model Hub config/routing
  consumers passed together: 303 cases in 11.37 seconds. Another run covered the
  eleven contracts plus 78 workflow/shard/private-fixture cases: 89 passed.
- Diagnostic-only mutations restored the old filesystem scope, disabled byte
  reuse, and disabled AST reuse separately. Each failed its targeted contract.
  The byte-reuse mutation initially revealed an insufficient test assertion;
  the contract now exercises both text and AST consumers of the same input.
- The original live checker caught a synthetic fixture version in the new,
  still-untracked test file. The fixture now generates its own version parameter;
  no version policy, file exclusion, or original assertion was weakened.
- A completed local fixed-check profile returned a clean verdict in 8.65 seconds,
  versus the original 28.04-second profile. It read 2,121 files exactly once and
  parsed 855 Python files once each; three additional AST calls came from literal
  evaluation. These local profiled samples do not establish hosted CI savings.
- Ruff 0.4.9, dependency integrity (83 packages), and whitespace checks passed.

Full PR CI and exact merged-source master CI remain required, including all 17
checks, every selected file/exit/metrics record, real distribution/install/upgrade
tests, Windows, and the UI browser gate. No additional optimization phase is
authorized by this plan.
