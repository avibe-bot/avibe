# Desktop test prerelease delivery

## Goal and ownership

Owner request (2026-09-22): complete the desktop test release workflow with macOS ad-hoc app signing in DMGs and unsigned Windows EXE installers. Deliver the implementation through PR #2062; executing a release, creating/pushing tags, merging into master, and deployment are not authorized.

Orchestrator: sessjyd34p9rp. Release implementation uses its own isolated worktree on feat/desktop-test-release-20260922, based on merge candidate 5589edc71292921281f95d072916628a9790fa6a. The existing integration Session sesy9cb4ht9nq retains its worktree and conflict validation. That Session's codex Agent was disabled at 07:58Z, so new release work is dispatched to the enabled backend-codex Agent. Do not edit the integration worktree or push either branch during implementation. Final integration into desktop is gated by independent review of the exact candidate and consuming tests.

## Contract and acceptance

Chosen minimal design: reuse .github/workflows/desktop-package.yml as a workflow_call build unit while keeping its manual artifact-only workflow_dispatch. Connect it to the EXISTING .github/workflows/release_ai.yml GitHub-only gh-vX.Y.ZrcN path. release_ai remains the ONLY publisher for that tag. Do not introduce a competing release workflow or another tag namespace. Official v* / PyPI behavior must remain unchanged, including draft-only AI notes and the existing finalizer. Do not retrofit desktop assets into immutable already-published tags.

Contract:
- Resolve and validate the gh-v rc tag and its exact source SHA; pass exact ref and a valid Tauri SemVer (e.g. gh-v3.1.2rc1 -> 3.1.2-rc.1) into reusable packaging. Ensure bundled Avibe version corresponds to that tag too, not a stale desktop manifest/default or caller branch. Check existing scripts/release_package_version.py first. Unknown/malformed tag fails before publication.
- Test channel uses macOS ad-hoc and unsigned Windows deterministically without Apple/Windows credentials, even if optional repository signing secrets exist. Preserve the old optional identity-signing MANUAL path. Clearly record actual layer: app ad-hoc, outer DMG unsigned/unnotarized; Windows installer unsigned. SIGNATURE is metadata, not a cryptographic signature file. No updater.
- Three required native targets: aarch64-apple-darwin + x86_64-apple-darwin DMG, x86_64-pc-windows-msvc NSIS EXE. Reuse existing build/runtime scripts. Verify existing action pins (desktop upload-artifact appears to differ from repository's v6 pin).
- All 3 target builds and runtime/hash metadata must succeed before GitHub prerelease finalize. Use unique target-qualified installer/metadata filenames or equivalent unambiguous packaging to avoid three runtime-manifest.json/SIGNATURE/SHA256SUMS collisions; raw DMG/EXE should be convenient release downloads. Validate expected asset set and hashes before upload/finalize, prove provenance matches exact tag/source. Failed/missing/mismatched assets leave draft/unpublished. Do not clobber changed desktop bytes under an existing tag: same bytes can be idempotent, different bytes require a new rc.
- Existing wheel/sdist, Show/Memory runtime assets and publication ordering remain intact. Correctly handle skipped desktop job for official tags in release job needs/if. Existing AI notes secret requirements are not Apple signing requirements; document real prerequisites without adding another notes service.
- Append stable test-installation guidance to gh-only notes and desktop README: platform/architecture downloads, hashes + metadata, macOS local app approval / Windows prompts as applicable, no blanket security disable, bundled Python/Node, manual app replacement upgrade retaining user data, removal behavior, links to #1976 for trusted distribution. Revise current artifact-only README claim.

Authorized files: .github/workflows/desktop-package.yml; .github/workflows/release_ai.yml; narrowly necessary desktop/scripts or scripts release helpers; existing/new focused release/package tests; desktop/README.md; docs/plans/desktop-test-release-20260922.md and a concise release runbook under docs if useful. No product UI, lifecycle redesign, auth, notifications, updater, or production changes.

Validation: existing release tests plus meaningful hermetic producer/consumer tests for tag/version/ref mapping, real assembled release assets/hash verification, all-target completeness, skipped official behavior, idempotent/same vs changed bytes, and preservation of manual packaging. Use mocked gh where publication logic requires calls; never hit a real Release write. Parse/lint workflow YAML and shell with existing tooling, verify referenced action revisions read-only. Run changed Python Ruff and focused runtime/merge tests already required. No demand for local full 3-OS packaging: clearly separate contract evidence from actual packaging/install acceptance.


## Evidence

Implementation, independent review, exact-head Codex/CI, and packaged/manual acceptance must be reported separately. No actual packaging or publishing result has been established by this design document.

## Implemented data flow and decisions

1. `release_ai.yml` resolves a canonical `gh-vX.Y.ZrcN` tag once to its peeled
   commit, a Tauri SemVer, and the existing helper's Python package version.
   Preview package/Memory builds and all three desktop targets check out that
   exact commit. The desktop producer also checks its HEAD against the tag.
2. The existing desktop workflow accepts `workflow_call` without receiving
   secrets. Before building the private Runtime it writes the tag-derived SCM
   environment overrides and Tauri config. The test override uses the ad-hoc
   macOS identity and clears Windows signing configuration. Manual dispatch
   keeps its optional complete Apple credential path and its SemVer input.
3. The existing Runtime builder remains unchanged. Its installed Avibe version,
   target, archive size/hash and tree hash are validated when recording assets.
   The app is ad-hoc signed and verified before the existing headless DMG wrapper;
   Windows checks Authenticode reports `NotSigned`.
4. Each target produces five uniquely named files: installer, Runtime manifest,
   source record, signature-state metadata, and checksums. The release consumer
   admits exactly 15 files, checks every checksum and semantic field, and appends
   the stable installation guide to the generated GitHub-only notes.
5. The existing GitHub-only upload shell compares all existing package, runtime,
   and desktop bytes before uploading anything. Published releases missing any
   desktop file are refused. Identical assets are reused; different bytes require
   a new rc. Runtime uploads still precede package/desktop uploads. All uploaded
   desktop assets are downloaded and compared again before the existing sole
   finalizer publishes the prerelease. Finalization is serialized per tag.
6. The release job explicitly permits skipped preview/desktop dependencies for
   official tags while requiring successful desktop/source jobs for TEST tags.
   The official notes step remains draft-only; `publish.yml` is unchanged.

No product lifecycle, UI, authorization, runtime protocol, updater, dependency,
or production configuration changes were made. Native outputs are not assumed
byte-reproducible; immutable comparison can require a new rc after a rebuild.

## Local verification, 2026-09-22

- 527 focused tests passed across desktop release, desktop Runtime bundle builder,
  release verification, package-version mapping, GitHub release state, and Show
  Runtime release tests. After adding actual producer-shell coverage for all
  three targets and renaming the version helper, all 39 desktop release tests
  passed (three additional cases; the other focused tests were unchanged).
- 207 existing Memory Runtime release, desktop Runtime/backend, Workbench-only
  Runtime, runtime ownership, and UI install tests passed. The 35 SQLAlchemy
  expression-index reflection warnings are existing warnings, with no failures.
- Consuming tests use the real Runtime ZIP writer, real desktop metadata producer,
  real assembled files, and the actual workflow upload shell. Only GitHub is
  simulated. They cover tag/annotated-tag/HEAD binding, invalid tags, stale package
  versions, archive tampering, target completeness, checksum and provenance
  mismatches, preserved manual signing export, identical/changed remote bytes,
  missing published assets, and corrupted uploads that must remain drafts.
- Ruff and `git diff --check` passed. YAML parsing and `bash -n` passed for both
  workflows. Cached actionlint v1.7.7 passed with a temporary configuration naming
  the repository's existing `macos-15-intel` and `windows-11-arm` runner labels
  (these postdate its built-in label list); no lint configuration was committed.
- Read-only public checks returned HTTP 200 for `action.yml` at all seven unique
  Action revisions referenced by these workflows. The previous desktop upload
  revision returned 404; it now matches the repository's working v6 pin.

## H4 and latest-master integration candidate

The implementation candidate now includes the real merge of upstream master
`335f62e6c15987d0ab72f6139c16fe49eea706e8` into the desktop release lineage,
preserving the six upstream Model Hub route-picker paths. It also contains the
bounded H4 readiness-loss correction recorded in the integration plan. Release
workflow, producer, consumer, and package files remain byte-identical to the
reviewed release implementation; the only release-plan changes are evidence
updates.

The shared RuntimeHost transition retains a valid started receipt and its
original launcher through three readiness misses, releases only completed
helper retry state, keeps pending helper deduplication, and classifies
unavailable readiness as `Unknown` while local liveness evidence remains. The
shell monitor invokes that transition after its existing generation/activity
fences. The candidate evidence is 96 RuntimeHost library tests, 28 bootstrap
consumer tests, 19 desktop shell unit tests, 27 shell boundary tests, and
`cargo fmt --all -- --check`; the focused shell test drives the same transition
used by production and proves started/reused recovery plus pending-helper
exclusion. Cargo emitted its existing shared-cache cleanup permission warning;
compilation and tests completed successfully.

## Remaining gates and handoff

- This lane delivers one local candidate commit on
  `feat/desktop-test-release-20260922`, descended from the release merge
  `5589edc71292921281f95d072916628a9790fa6a` and the latest-master merge
  above. No push, PR, tag, dispatch, Release mutation, merge into master,
  deployment, or Watch changes were performed.
- The orchestrator independently accepted integration evidence commit
  `33eeb3eefbeecf0a479f4f70b922f900466102fb`; its code is identical to this lane's
  base and its additional plan evidence will be integrated by the orchestrator.
  This lane did not modify that integration worktree or contact its Session.
- Independent candidate review, final integration, exact-head Codex review, zero
  unresolved PR threads, and CI remain the orchestrator's delivery gates. Local
  checks do not substitute for those gates.
- Actual three-target packaging and installation remain unexecuted: build an
  owner-authorized fresh rc; verify each downloaded DMG/EXE and signature layer;
  launch on machines without Python/Node; verify readiness/onboarding, manual
  replacement upgrade preserving user data, and uninstall cleanup/preservation.
  The existing AI notes secret and Actions/repository permissions are still
  prerequisites. Apple and Windows signing credentials are not TEST prerequisites.

### Pre-push master refresh

At 08:31Z the explicit remote fetch found `master` advanced from `16bf1be3` to
`a582c87f0156082f87548be2912e063f09fc2bb1` (#2112), while remote `desktop`
remained `8440cf81737be5db46a16992ecce41fe29beb866`. Push authorization for
`da11a508bcb4f9602b8a368a2a1fc4f46fbd9ed9` was therefore held. The new master
changes only pytest configuration comments, the advisory async-plugin guard,
its consuming tests, and the bounded atomic-IO test reader. These four files
merged without conflicts; all eight release implementation files were unchanged
before this evidence update. The merged tree passed 750 tests (seven existing
SQLAlchemy reflection warnings) across those new tests, six release suites,
desktop bundle builder, desktop Runtime, IPC, and UI installation consumers.
The parent process HOME/XDG/backend state paths were redirected into a temporary
test directory as well as the repository's per-test isolation. Ruff passed for
the release changes and incoming Python files. The updated local candidate is
returned for independent inspection before any push. All five historical PR
threads remain resolved, no review is pending, and the original lane Watch
`54efbc6d5cec` remains live with its cursor unchanged under Session
`sesu7hnukugyr`; orchestrator Watch `25111cdab6a3` remains separate.

## Owner authorization and next TEST release

At 2026-09-22 08:34:44Z the owner explicitly authorized publishing the desktop
TEST prerelease after review passes. This supersedes the earlier no-publication
boundary for this conditional GitHub-only TEST release; merging into master is
still not authorized. The exact final desktop commit must have Codex PASS, zero
unresolved threads, all expected `lint` and `desktop-shell` checks green, clean
mergeability, and latest-master ancestry. The orchestrator independently verifies
that gate before executing or delegating the release tag push. No further owner
confirmation is required for that authorized sequence.

The intended next tag is `gh-v3.1.1rc4` (desktop `3.1.1-rc.4`, bundled Avibe
`3.1.1rc4`), subject to a fresh remote tag/Release lookup immediately before
allocation. If another publisher uses it first, choose the next unused rc in the
same sequence. Do not reserve or overwrite a tag or existing asset. Create an
annotated tag on the exact reviewed desktop commit with the following annotation:

```text
Desktop TEST prerelease: macOS ad-hoc app in unsigned/unnotarized DMG; unsigned Windows x64 installer.

<!-- avibe:update-notification=none -->
```

Publication runs only through the tag-triggered `Release (AI Notes)` workflow as
a GitHub prerelease with `latest=false`; no manually created competing Release,
official `v*` tag, PyPI publish, production service change, or local service
restart is authorized. Apple/Windows signing credentials are not required. The
orchestrator confirmed the existing `OPENAI_API_KEY` secret name is present,
without reading its value.

Before any tag push, arm a separate exact-source/tag Actions Watch through
`background-watch-hook` for `Release (AI Notes)`, with independent state and both
timeout layers disabled. Confirm liveness and explicitly diagnose a missing or
failed workflow. Preserve the two existing PR Watches/cursors until the PR gate
report; establish durable release observation before retiring the lane PR loop.

Completion requires a successful workflow for the exact source/tag and all three
public native installers plus matched provenance, hashes and signature metadata,
verified by download/readback. Report direct URLs for Apple silicon and Intel
DMGs and the Windows x64 EXE. Contract tests are not native packaging evidence.
Actual install, manual replacement upgrade and uninstall remain separate manual
acceptance; never install over the owner's live Avibe to claim those passed.
Any packaging defect requires a bounded fix, new exact-head review, and a fresh
rc when immutable bytes would change, rather than blind reruns or asset replacement.

### Latest master ancestry refresh

The explicit remote refresh on 2026-09-22 found `origin/master` at
`4b964fef223862c2cf7d5cec852ac0d3aa64e323` (#2118), ahead of the previously
integrated `335f62e6c15987d0ab72f6139c16fe49eea706e8`. That upstream change is
limited to `ui/scripts/lintBaseline.test.mjs`, where the temporary integrity
probe now carries cleanup guidance and verifies exactly one inline-policy
diagnostic. It has no release, RuntimeHost, shell, or product behavior delta.
The change was merged as a real conflict-free second parent into the desktop
candidate. Focused Vitest evidence passed 33 tests; the new exact-head GitHub
lint and desktop-shell gates remain required before push and TEST publication.

### H5 candidate: lane handover, master refresh, local evidence

- 2026-09-23: The previous implementation lane (Session `sesu7hnukugyr`, codex
  backend) is dead — every run failed with `model_hub_recovery_exhausted` and a
  resume failed again immediately. A replacement lane inherited its worktree and
  its uncommitted H5 draft rather than resetting the branch; the draft's two
  Python fixes were kept and its Rust test was replaced with a real consuming
  test. Root-cause detail for all three H5 threads lives in
  `docs/plans/desktop-master-integration-20260920.md`.
- **Latest master.** An explicit refspec fetch put `origin/master` at
  `c400da2df4eb09feab84678c8f3d5c76d3af1b12`, ahead of the previously integrated
  `4b964fef223862c2cf7d5cec852ac0d3aa64e323`. The incoming delta is Model Hub UI
  and Claude model catalog work across 37 files; it touches no desktop shell,
  RuntimeHost, or release path. It was merged as a real conflict-free second
  parent, and `origin/master` is now an ancestor of the candidate. The PR's
  `baseRefOid` was not used for this: it was stale.
- **TEST release surface unchanged.** The seven release implementation files
  from `13082a8501ce5d1e771f56395a72fc062859cf8b` — the two workflows,
  `desktop/README.md`, `docs/desktop-test-installation.md`,
  `scripts/desktop_release.py`, `tests/test_desktop_release.py`, and
  `tests/test_release_verification.py` — were re-verified byte-identical by
  object hash after the merge. No UI or workflow file is touched by this
  candidate's own commit.
- **Local validation on the candidate tree.** 107 focused Python tests pass
  (`tests/test_desktop_runtime.py` in full, plus the reconcile/model-hub
  selection from `tests/test_local_deps.py`), all under the repository's
  per-test `HOME`/XDG isolation. `ruff check` is clean on the four changed
  Python files. The Rust workspace passes `cargo fmt --all --check`, 201 tests
  under `cargo test --workspace --all-features` — including the shell unit tests
  and the `shell_boundaries.rs` integration guards — and `cargo clippy
  --workspace --all-targets --all-features -- -D warnings`. GitHub `lint` and
  `desktop-shell` at the pushed head remain the authoritative gates.
- Push is held pending explicit orchestrator candidate clearance. Replies to the
  three H5 threads follow the pushed head, not this commit.

### H6: the Windows-only path-separator defect the first TEST release exposed

The first real prerelease attempt found a defect no existing gate could have
caught. Tag `gh-v3.1.1rc5` at the reviewed head started `Release (AI Notes)` run
`35776410677`, and `desktop-packages / x86_64-pc-windows-msvc` failed at **Build
Workbench**, so no Windows installer could be produced and the release could not
complete.

- **Cause.** `ui/scripts/validate-out-of-tree-imports.mjs` guards the Docker
  `ui-builder` stage: every import that escapes `ui/` must be placed at the
  repository-relative path the import expects, or `npm run build` cannot resolve
  it in that image. The guard produced its targets with
  `path.relative(REPO_ROOT, resolved)`, which answers in the *host* separator.
  Everything the target is then compared against is POSIX: the Dockerfile's
  `COPY` sources, the ``target.startsWith(`${source}/`)`` prefix test, and the
  `path.posix` joins. On Windows the target became
  `vibe\data\api_key_vendors.json`, which equals no `COPY` source and prefixes
  none, so `imagePathOf` returned nothing and the guard reported that the file is
  put "at no path at all" — against a Dockerfile that is correct.
- **Why every other job was green.** The guard only runs inside `npm run build`,
  and `ui-checks` runs on Linux only. The desktop Windows package job is the one
  place in the pipeline where `npm run build` executes on Windows, so the first
  Windows packaging attempt was the first execution that could fail.
- **Fix.** The repository-relative path is normalized to POSIX at the single
  place it is produced, so the whole comparison chain stays POSIX.
  `repoRelativePosix` lives in `ui/scripts/repoRelativePosix.mjs` and takes the
  path flavour as a defaulted argument; `escapingImports` calls it instead of
  `path.relative`. The validator is not restructured, the check is not weakened
  or skipped on Windows, and the Dockerfile is untouched.
- **Why a separate module rather than an export from the validator.** The
  validator runs its validation at import time and calls `process.exit(0)` when
  no Dockerfile is present — a state its own comment anticipates inside the
  `ui-builder` stage. Importing it from a test would make that a silent
  green-exit hazard for the whole suite. The repository's dominant script idiom
  is already a pure module beside its entry point, as `scenarioCatalog.mjs` is to
  `validate-scenario-catalog.mjs`. A `main`-module guard was rejected for the
  opposite reason: a guard that compares `process.argv[1]` to
  `import.meta.url` can disagree on Windows drive-letter casing, which would
  silently stop running the guard on the one platform it now has to protect.
- **Consuming test.** `ui/scripts/repoRelativePosix.test.mjs` drives
  `path.win32` explicitly, which is the only way to reach Windows' separator from
  Linux CI, where the test actually runs. Six cases pin the normalization, the
  POSIX no-op, the host default, and the failure itself: for each of the two real
  catalogs the Dockerfile copies, the un-normalized Windows target matches its
  `COPY` source by neither equality nor prefix while the normalized one matches,
  and the expected image path is `/app/vibe\data\api_key_vendors.json` before the
  fix against `/app/vibe/data/api_key_vendors.json` after. Removing the
  normalization fails four of the six, with exactly the string the Windows job
  reported.
- **Local validation.** The full UI suite passes at 348 files and 5295 tests, up
  by one file and six tests. `npm run build` succeeds, which runs the repaired
  guard as its first step. `npm run lint` reports no drift in any (file, rule)
  pair, and ESLint is clean on all three changed files. No Python changed, so
  Ruff does not apply. The seven release implementation files from
  `13082a8501ce5d1e771f56395a72fc062859cf8b` were re-verified identical.
- **Release state.** Tag `gh-v3.1.1rc5` stays as it is and is never reused; the
  orchestrator cuts a new rc after this fix passes review and CI. Push is held
  pending explicit candidate clearance.

## H7 — the failure-path review round

The exact-head Codex review of `b85261e41` returned five P2 findings. Four of
them are one root-cause class: a multi-step operation commits an irreversible or
externally visible step before the step it depends on is known to have
succeeded, and the failure path neither aborts nor undoes it. That class had
already appeared at the H5 head, so the review-loop circuit breaker was tripped
and the inventory went to the orchestrator, which issued the scope ruling this
round implements. Each fix is expressed as one decision function that owns every
outcome and emits explicit effects, so the failure path is a branch of the same
unit and a test can assert the effect sequence.

- **Stale UI replacement now honours the stop result.** `start_ui` discarded
  `stop_pid`'s answer, unlinked the pid file and spawned a replacement anyway. If
  the stop failed, the old process still owned the listener, the replacement
  could only die on bind, and the pid record had stopped naming the process that
  actually had to be killed. It now fails through the module's existing idiom —
  an error log and a falsy return — before the unlink, so the record keeps
  naming the live process. H5 is what made this branch busier: reclassifying
  `runtime_identity_invalid` as incompatible routes more restarts through it.
- **Uninstall restores the login item it cleared when the removal does not
  happen.** Clearing **Start at Login** still comes first, so the OS never keeps
  launching an application the user just removed. But a removal that reports
  `Ok(false)` or errors leaves the application installed, and the user's login
  preference was silently gone. The decision function is now async and owns the
  removal outcome, so the restore is a branch of the same unit rather than a
  guard bolted onto the caller.
- **Readiness recovery stops notifications only after the hand-off succeeds.**
  The monitor tore the connection down before `return_to_bootstrap` was known to
  have navigated. A failed navigation plus a recovered Runtime left native
  notifications dead for the rest of the session with nothing to restart them.
  The navigation is now `restore_bootstrap_view` and the recovery decision owns
  the ordering; `return_to_bootstrap` keeps its previous behaviour for its five
  other callers.
- **Release tooling runs from the released source.** Both the
  `resolve-desktop-release` and `release` checkouts passed only `fetch-depth`,
  so a `workflow_dispatch` resolved and published a tag using whatever branch
  the dispatch selected. Both are pinned now. The release job needs a fallback
  because `resolve-desktop-release` is gated on `gh-v` and is skipped on the
  official `v*` path, where its output would be the empty string. `Checkout
  release verification` keeps `github.sha` deliberately: it is workflow-owned
  verification applied to the tagged build, and pinning it to the resolved
  source would defeat that.
- **Deferred.** Uninstall deletes the private Runtime root without fencing an
  in-flight backend install. The install lock is Python-only, so the Rust
  launcher has none to take; the fix is a cross-language ownership change
  outside this PR. Tracked in issue 2131 and in the PR's Known-by-design ledger.

- **Local validation.** The new checkout-pinning guard is mutation-checked
  against both an unpinned checkout and a pin that loses its official-path
  fallback. The `start_ui` consumer fails with its own message when the fix is
  reverted, and the two Rust effect-sequence tests fail when the stop is moved
  back before the hand-off or the restore is dropped. Full Rust workspace: 204
  tests including all 27 `shell_boundaries.rs` guards, `cargo fmt --all --check`,
  and `cargo clippy --workspace --all-targets --all-features -- -D warnings`
  clean. 303 Python release and desktop-runtime tests, Ruff clean on the changed
  files, and actionlint on both release workflows with only the three
  pre-existing newer-runner-label notices.
- **Release state.** The freeze lifted for `.github/workflows/release_ai.yml`
  only; its delta against `13082a8501ce5d1e771f56395a72fc062859cf8b` is the two
  pins and their rationale. The other six release implementation files are still
  byte-identical, which is why the guard test lives in its own file rather than
  in `tests/test_desktop_release.py`. Tag `gh-v3.1.1rc5` stays spent. Push is
  held pending explicit candidate clearance.

## H8 — packaging observability after the second TEST prerelease

`gh-v3.1.1rc6` ran the desktop packaging matrix from `902c447141df` and failed on
two of three targets, so nothing was published. Desktop packaging has never run
outside this PR — neither `.github/workflows/desktop-package.yml` nor
`desktop/scripts/build-runtime-bundle.py` exists on master — so these are first
exposures rather than regressions, and more should be expected.

### What the Windows job showed, and what it did not

The private-Runtime probe failed because the service process exited before
acquiring the service lock, about 28 seconds after `vibe start` spawned it. Why
it exited is **not** in the job log, and cannot be: the probe runs with its
entire HOME inside a `TemporaryDirectory`, so `runtime/service_stderr.log` and
`logs/vibe_remote.log` are written and deleted without ever being read. The job
instead reported a `PermissionError` on a payload DLL — the last of four stacked
exceptions and the least informative.

Four separate defects turned one failure into an unreadable one, and each is
fixed here:

- **The probe's own logs were discarded.** `verify_payload` now reports every
  diagnostic file under the probe HOME to stderr before the directory is
  removed, naming the files that are absent as well as the ones that exist.
- **The failure path orphaned processes.** Nothing killed the spawned service or
  UI. `vibe/runtime.py` returns the UI pid even when its health checks fail —
  pre-existing behaviour, deliberately left alone in a release round — so a live
  UI kept holding the log handles that made the temporary directory's removal
  fail. The probe now terminates whatever its pid files still name.
- **The `finally` replaced the real exception.** `vibe stop` ran with a 60s
  timeout raised straight out of `finally`, superseding the `CalledProcessError`
  that carried the actual failure, with both streams at DEVNULL so the stop's own
  account was discarded too. `stop_private_runtime` now reports its outcome
  instead of raising it, and prints what it captured.
- **Cleanup noise masked the result.** Both `TemporaryDirectory` uses now pass
  `ignore_cleanup_errors=True`, so a leftover Windows handle leaves a stale
  directory rather than becoming the exception the job reports.

The authoritative proof is the next CI run. That is the point: this round buys
legibility for an unknown number of remaining first-exposure failures rather
than guessing at this one. The one added test covers the real seam —
`collect_probe_diagnostics` — and is mutation-checked against dropping the tail
slice and against skipping absent files silently. The subprocess orchestration
is deliberately not harnessed.

### macOS DMG

`hdiutil create` failed with "Resource busy" on `aarch64-apple-darwin` while the
identical script succeeded on `x86_64-apple-darwin` in the same run, so this is
imaging flake, not a code defect. It now retries up to three times with a pause,
and the final failure is still a build failure.

The signature detection beside it was broken: `sed -n 's/^Signature=adhoc$/p'`
has no replacement field, so sed errored on every invocation and `adhoc` was
always empty, leaving the branch to be decided by `-z "$identity"` alone. The
expression is now `sed -n '/^Signature=adhoc$/p'`.

**No case changes behaviour.** `Signature=adhoc` and `Authority=` are mutually
exclusive in `codesign` output — an ad-hoc signature has no certificate chain and
prints no Authority line — so a correctly detected ad-hoc bundle was already
taking the ad-hoc branch through the empty-identity test. Checked against all
three real output shapes: ad-hoc, Developer ID, and unsigned each select the same
branch before and after. It is a correctness and noise fix that finally makes the
code do what the comment above it has always claimed.

The same broken expression also appears in `.github/workflows/desktop-package.yml`
at the "Verify macOS app signature matches the signing path" step. That file is
in the frozen release-implementation set and is **not** touched here. The same
analysis applies — its `adhoc` is always empty, the identity branch still turns
on `-z "$authority"`, and the TEST branch uses `grep -q` directly rather than the
sed — so it is noise with no behavioural consequence, reported to the
orchestrator rather than fixed under the freeze. **Deferred by orchestrator
decision, to be fixed in a later round**: touching a frozen release-implementation
file to correct cosmetic noise is not worth the re-verification it would cost
this close to a TEST prerelease.

### The stop report's own timeout branch

Review of the change above caught the one place where the fix reproduced the
defect it was written to remove. `subprocess.run` decodes its streams on the
completion path, which a timeout never reaches: `TimeoutExpired` carries what it
captured before the deadline as raw bytes even when the call passed `text=True`,
and an unwritten stream as `None`. `_stop_report` would have joined bytes into a
string and raised `TypeError` — from inside the `finally`, over the probe failure
the report exists to explain.

This is not hypothetical for the next run. The Windows job that motivated the
whole round took exactly this branch: `vibe stop` ran the full sixty seconds and
timed out. `stop_private_runtime` now normalizes both streams before formatting
them, decoding the same way `collect_probe_diagnostics` does so an undecodable
byte is reported rather than raised.

Only two subprocess calls in the file carry a timeout at all, and the other one
— `taskkill` in `terminate_probe_processes` — never reads its streams, so it does
not share this defect. It does carry a related one: it has no `TimeoutExpired`
handler and runs inside an `except BaseException:` block, so a thirty-second hang
there would replace the original exception and mask the failure the same way.
Different root cause, same consequence. **Deferred by orchestrator decision, to
be fixed in a later round** alongside the `desktop-package.yml` expression above.

### One opener for the notification connection

The notification connection's lifetime was not paired with the outcome of the
navigation it belongs to. `open_workbench` opened it before attempting the
navigation, and every failure exit from that loop — no window, no bootstrap
navigation, `set_active_origin` refusing, `window.navigate` erroring — returned
without closing it, leaving it consuming Runtime events and able to raise native
notifications while the shell sat on bootstrap.

The fix is an ownership statement expressed as a deletion rather than a guard.
`Notifications::start` returns early when a connection for the same origin
already exists, so the monitor's own start was a no-op after it and the early one
was redundant on the success path and a leak everywhere else. Removing it leaves
exactly one opener, `start_runtime_monitor`, which is reached only once a handoff
has actually completed; the stop sites already give the connection up only where
a hand-off away actually happened. Start and stop now both hang off a navigation
that really occurred instead of one that was merely attempted, and the rule is
written at the monitor's start site for the next reader.

The shell's boundary test already owned this invariant and had recorded the old
shape: it required `open_workbench` to contain the early start. Both starts
arrived in the same commit, so what it captured was the code as first written
rather than a decision that the connection must open early — and its own name
says the lifecycle follows runtime ownership, which is exactly what the early
start broke. It now requires the start inside `start_runtime_monitor`, requires
`open_workbench` to touch the lifecycle not at all, and holds the opener count at
one, so reintroducing a premature start anywhere fails it.

The line predates this PR — it arrived with #1983 — and the other lifecycle sites
(`exit_shell`, `stop_runtime`, the readiness-loss recovery, `return_to_bootstrap`
and the removal flow) were audited against that one sentence and do not
contradict it. None of them is changed here.

## H9 — the lease check that could not pass on Windows

The third TEST prerelease, `gh-v3.1.1rc7`, failed its Windows desktop package in
the private-Runtime probe. The packaging observability added in H8 is what made
it readable: the probe printed the service's own log, which said `Failed to
start: Unsafe native lease file`, and the `RuntimeError` about the service lock
that the job surfaced first was only the symptom of that.

`NativeCredentialLease.acquire` verified the lease file with a POSIX permission
mask, `info.st_mode & 0o077`. Windows synthesizes `st_mode` from file attributes,
so any writable regular file reports `0o100666` there and the mask always yields
`0o066`; the `0o600` passed to `os.open` cannot change it, because Windows honors
only the read-only bit from that argument. The check therefore rejected every
lease on Windows, deterministically, and the service could not finish starting.
The clause is now POSIX-only, in the idiom the same expression already used twice
within five lines — `hasattr(os, "getuid")` for the owner check and
`getattr(os, "O_NOFOLLOW", 0)` for the open flag. The equivalent guarantee on
Windows is an ACL check, not a mode mask, and is not attempted here.

**This arrived from master, not from us.** The check came in with #2060
(`162fda594`, 20 September); this PR does not touch `core/backend_restart.py` in
any other respect. It stayed invisible because nothing had ever executed that
line on Windows: unit tests run on Linux, the lease tests needed no platform
skip, `desktop/scripts/build-runtime-bundle.py` does not exist on master, and no
prior prerelease shipped a desktop installer at all. The Windows private-Runtime
probe this PR introduces is the first caller to reach it on that platform, which
is why a pre-existing defect surfaced as a release-path blocker here. It was
fixed in this PR rather than deferred for exactly that reason: no number of
re-runs produces a Windows installer while it stands.

Whether an ordinary Windows service start also reaches `acquire()` — and has
therefore been broken on master since 20 September — is deliberately not claimed
here. It is tracked in issue #2132 with this evidence. The coverage added with
the fix asserts both directions, so neither dropping the mask nor restoring it
unguarded can pass: POSIX still refuses a group-accessible lease file, and the
identical file acquires once `os.name` is not `posix`.

## H10 — Known-by-design: runtime identity across lifecycle boundaries

- **Deferred.** The Codex review at `4458203f1a` returned three P2 findings —
  an unscoped handover stop in `launcher.rs`, monitored readiness collapsing a
  changed `desktop_runtime_id` to `true` in `bootstrap.rs`, and an uninstall
  recovery in `lib.rs` that can leave no monitor owner when its navigation
  fails. All three are one cause: the desktop lifecycle re-derives which
  Runtime it has authority over at every boundary instead of carrying one
  verified identity through launch, probe, monitor, stop and removal. The same
  class was patched per-boundary on roughly seven earlier heads of this PR, so
  a bounded patch at a seventh boundary was explicitly rejected under the
  review-loop circuit breaker in `AGENTS.md`; the fix is a coordinated
  ownership change spanning the Rust shell, `runtime-host` and the Python
  runtime. None of the three is on the packaging path, none risks data loss or
  user-state corruption, and each requires a second desktop instance replacing
  the Runtime mid-flight — a race a TEST prerelease installer does not need to
  survive. They do block a master merge, which this PR is not authorized to do.
  Tracked in issue 2135.

## H11 — the control IPC owner check under an elevated token

`gh-v3.1.1rc8` proved the H9 lease fix: Windows got past `Unsafe native lease
file` and reached `IM runtime ready`, then failed one layer deeper with
`control IPC path is not owned by the current user`, which stopped the runtime
through the lost-lease path.

The cause is Windows object ownership, not a path problem and not another POSIX
assumption. `_WindowsSecurity` builds its expected descriptor from the token
**user** SID — `self.sddl = f"O:{self.current_user_sid}..."`, fed by
`_read_current_user_sid()` reading `TokenUser` — and then requires the existing
directory's owner to equal that SID. But Windows stamps a newly created object
with the token's **default owner**, and for an elevated token that default is
`BUILTIN\Administrators`, not the user. An elevated Avibe therefore creates a
runtime directory it then refuses to recognise as its own.

Two independent owner checks had to move together, which the first reading of
this bug missed. `secure_existing_owned_path` gates on
`_named_path_owner_matches`, and then finishes with `validate_path` →
`_validate_security_descriptor`, which re-checks the owner itself. Because
`SetNamedSecurityInfoW` is called with `None` for the owner argument — repair
rewrites the DACL and never the owner — relaxing only the first check would have
left the second one failing on exactly the same directory. Both now route
through one predicate, `_owner_is_self`, so there is a single answer to "did
this process create this path".

Acceptance is derived from the running token, not from a hardcoded well-known
SID: an unelevated process still accepts only its user SID, and
`BUILTIN\Administrators` is accepted only when it is genuinely this token's
default owner.

**Tradeoff, stated rather than slipped in.** An elevated Avibe now trusts an IPC
runtime directory owned by `BUILTIN\Administrators`, which means any
administrator on the machine could have created it. The DACL contract is
deliberately unchanged — still present, still protected, still exactly
`(A;;FA;;;<user>)(A;;FA;;;SY)` — so an Administrators-owned path is still
rejected unless its access control list grants nobody but this user and SYSTEM.
For an elevated Windows service that matches normal practice, where
administrator-equivalence is already assumed; without it an elevated Avibe
cannot use control IPC at all.

The Linux-executable tests pin the acceptance rule by driving `_owner_is_self`
and `_validate_security_descriptor` through a bypassed constructor with fake
`advapi32` bindings. They prove the decision logic, including that an unrelated
SID and a non-protected DACL are still rejected. They cannot prove the Win32
behavior underneath; as with H9, only the next prerelease's Windows leg settles
that.

## H12 — CLI discovery must not run on an async request path

The runtime path projection added in this PR made `to_app_config` call
`resolve_cli_path`, whose last-resort branch runs `npm config get prefix` with a
five-second timeout and no caching. `to_app_config` is reached from async
handlers — `_reconcile_platforms` on the controller loop, `opencode_options_async`
and `_opencode_get_server` on the UI server loop — so a missing backend CLI would
stall an event loop for up to five seconds, once per missing backend.

Fixed by passing `include_npm_global=False`: that branch is the only one in the
resolver that spawns a process, and the cheap candidates it keeps —
`~/.local/bin`, `~/.bun/bin`, Homebrew, `/usr/local/bin` and every NVM version —
already cover what a GUI-launched Runtime needs. The cost is narrow and stated:
a CLI installed via `npm -g` into a non-default prefix that is *also* absent from
`PATH` will no longer be discovered by the Runtime. A GUI-launched process is
exactly the context where such a prefix is unreliable anyway.

Memoizing the npm prefix was the alternative and is worse: it still blocks the
loop once and buys a staleness question that the keyword does not.

## H13 — Banked: why the Windows service lock self-check fails

Not fixed. Windows is shelved; this is recorded so the next attempt starts from
a cause rather than from the logs again. Tracked in issue 2141, which is the
layer underneath issue 2132: both are code that is correct on POSIX because
POSIX file locks are advisory, and wrong on Windows because Windows byte-range
locks are not.

The rc8 Windows leg reported two symptoms that look separate and are one bug:
the probe said pid 3176 had not acquired the service lock after 5s, and 47
seconds later the service stopped itself through `_stop_for_lost_lease`. The
control IPC owner error in that window is on the shutdown path, i.e. downstream.

The cause is that the lock byte sits on top of the data it guards.
`_try_lock_file` seeks to 0 and calls `msvcrt.locking(fd, LK_NBLCK, 1)`, so it
locks byte 0. `_lock_file_pid` then seeks to 0 and reads — the same byte. On
POSIX this is harmless because `fcntl.flock` is advisory and never blocks a
read. On Windows byte-range locks are **mandatory and per-handle**: Microsoft's
documentation states that if the locking process attempts to access a locked
byte range through a second file handle, the attempt fails, and that an
exclusive lock denies all other processes both read and write access to the
range. Every read of that byte through any other handle therefore fails with
`ERROR_LOCK_VIOLATION`.

Both symptoms follow directly:

- The external probe calls `read_service_instance_lock_record()`, which opens
  the file fresh and reads byte 0. The read raises, the `except OSError` returns
  `None`, and the probe concludes the lock was never acquired — while the
  service is in fact holding it.
- `current_process_owns_service_instance()` calls `service_lock_held_by`, which
  opens a **second handle in the same process**. Its `_try_lock_file` correctly
  fails, and then `_lock_file_pid` reads the locked byte through that second
  handle and also fails, yielding `None`. `None == os.getpid()` is false, so a
  process that genuinely owns the lock reports that it does not, and
  `_stop_for_lost_lease` shuts the service down.

The smallest fix is to stop overlapping the two: on Windows, lock a sentinel
byte at a fixed offset past any plausible record instead of byte 0, leaving the
JSON payload readable by every handle while the lock is held. POSIX keeps
`flock`, which is whole-file and unaffected. The compatibility caveat to decide
before shipping it: a process locking byte 0 and one locking the sentinel byte
do not exclude each other, so a mixed-version transition needs the old service
stopped first.

This is reproducible on `windows-latest` without any packaged Runtime — it needs
one lock file, two handles and a read — so an existing Windows CI job could hold
the regression test whenever this is picked up. Two related notes found while
diagnosing and deliberately left alone: `storage/lock.py::_try_lock` has the
same overlap shape for the migration lock, and the `_stop_for_lost_lease` log
line claims the service "no longer owns" the lock, which asserts a transition
nobody observed — it should say the ownership check failed.

## H14 — fixing the Windows service-lock blackout, red first

Windows was reopened by owner decision to fix H13. The evidence was built before
the fix, as a test that fails on Windows and passes on POSIX, wired into the
existing `windows-control-ipc` job in `.github/workflows/lint.yml` — one lock
file and two handles, no packaged Runtime, no installer, no new workflow. That
job runs `tests/test_service_lock_record.py`, whose cases go through the real
`vibe.runtime` entry points rather than a synthetic `msvcrt` demonstration, so a
red there is the product defect and not a restatement of the platform fact.

### The premise, verified rather than assumed

The fix is Windows-only, which is safe exactly to the extent that no Windows
service can be running today. That was checked against the code rather than
inferred from the rc8 log:

- `main.py` acquires the service lock unconditionally at startup, so every
  Windows service takes it.
- `RuntimeWorkSupervisor`, the watch service and the scheduled-task service each
  capture `service_instance_lock_attached_to_process()` at construction, which is
  true in a real service, and then poll `current_process_owns_service_instance()`.
- That call re-opens the lock file, so on Windows it reads the record through a
  handle that is not the one holding the lock, gets `None`, and answers `False`.
- `RuntimeWorkSupervisor` is constructed with
  `on_lease_lost=lambda: self.request_shutdown("service lease lost")`, so the
  first such answer shuts the whole service down.

There is no branch that skips the lease check for a real service, and no path by
which the record becomes readable while the lock is held. A Windows service
therefore cannot stay up on any released version, which is why moving the locked
byte cannot break a working installed base: on Windows there is none, and POSIX
is not touched.

### The fix

`vibe/runtime.py` now locks `_WINDOWS_LOCK_BYTE_OFFSET = 1 << 30` instead of byte
0, applied to the raw descriptor through `_windows_lock_byte` because a text
handle cannot seek to an arbitrary byte, and restoring position 0 afterwards so
the buffered handle stays coherent for the record write and read around it.
Locking past end-of-file is legal on Windows, costs no disk and does not extend
the file. The record itself is untouched: it stays a plain JSON document from
byte 0, which is what keeps `read_service_instance_lock_record` and a human
running `type service.lock` working. The alternative — keeping the lock on byte 0
and starting the record at byte 1 — was rejected for exactly that reason and is
pinned against by a test.

The POSIX branch is unchanged, and a test asserts it: the advisory whole-file
`flock` is taken with the descriptor at offset 0, so the platform that does have
an installed base keeps excluding today's releases byte for byte.

### `storage/lock.py::_try_lock` — reachable, different consequence, left alone

The same overlap shape is there, and it is reachable on Windows, but the
consequence is not the same and fixing it the same way would be a net loss:

- The migration lock's exclusion works correctly on Windows today. A waiter's
  `_try_lock` fails, which is the right answer, and it waits and retries. Only
  `_recorded_holder` reads the locked byte, and it feeds one log line —
  "held by pid unknown" instead of a pid. Its own docstring already says nothing
  decides on that value.
- Unlike the service lock, this path **works** on Windows, so it does have an
  installed base. It is taken during startup before any lease check, and by plain
  `vibe` CLI commands that never become a service.
- Moving its byte would therefore create a real old-versus-new window in which two
  processes both hold "the" migration lock and migrate the same SQLite database
  concurrently. Trading a corrupt-database risk for a log line that already reads
  as an honest "we do not know" is the wrong trade.

Recorded here rather than fixed. The two other Windows `msvcrt` lock sites,
`core/managed_runtime.py` and `core/show_runtime.py`, were checked and do not
have the defect: they use the lock file purely as an exclusion token and identify
it with `fstat`/`lstat`, never by reading its bytes.

### The log lines that cost three release candidates

`_stop_for_lost_lease` said the process "no longer owns the service lock", a
transition nobody observed, and `core/watches.py` said the same thing in its own
words. Both now say what was actually established — re-reading the lock did not
confirm this process as its holder, either because the lock went away or because
the record could not be read. The watch-service copy was corrected in the same
round on the same reasoning that keeps `storage/lock.py` and its twin together:
fixing one instance of a defect and leaving its duplicate is how the duplicate
survives.

### What the tests prove, and what they do not

They prove that while the lock is held, its record is readable, the holder is
named, the holder recognises itself through a second handle, a launcher can see
the service it spawned, and the started phase is visible to a watcher — on
Windows and POSIX alike. They prove the lock still excludes, and that POSIX still
locks the whole file advisorily at offset 0.

They do not prove that a Windows service now starts end to end: that needs the
packaging probe, which is a separate decision. They do not cover a mixed-version
Windows transition, because the premise above says there is nothing to transition
from. They say nothing about the migration lock, deliberately.

## H15 — control IPC hardening orphaned the runtime directory

`gh-v3.1.1rc9` failed on `desktop-packages / x86_64-pc-windows-msvc` in "Build
verified private Runtime". The whole private runtime tree went unreadable to the
process tree that created it: the service lock, both captured stdio logs and the
model-hub tree, the last of these with an explicit `[WinError 5]`. `release` was
skipped, so no rc9 release object exists.

### Mechanism

`core/control_ipc.py` hardens the directory containing the endpoint descriptor,
and that directory was the runtime directory itself — `write_descriptor_atomic`
passed `target.parent` to `_ensure_private_directory(..., allow_repair=True)`,
where `target` was `get_runtime_dir() / "control-ipc.json"`.

`SetNamedSecurityInfoW` with `PROTECTED_DACL_SECURITY_INFORMATION` propagates.
The DACL it applies carries no inheritable ACE, so the propagation strips the
inherited ACEs from every child that already exists. Children created purely by
inheritance hold no explicit ACE of their own and are left with an effectively
empty DACL — unreadable even to their owner's later `open()`.

The discriminating observation is what stayed readable. `runtime/status.json`
survived and was the newest file in the directory, because `write_json` goes
through `write_atomic`, which creates a fresh `mkstemp` file and `os.replace`s
it: a child created *after* the stamp takes the creating token's default DACL
rather than parent inheritance. `logs/vibe_remote.log` survived because it is in
a different directory. Timing agrees: uvicorn started at 04:40:55,077 and the
first `PermissionError` landed at 04:40:55,097.

This never fired before `03662baa7`, which taught the owner check to accept an
elevated token's default owner. Until then the check raised before reaching
`SetNamedSecurityInfoW`. The fix was correct; it made a latent defect reachable.

### The boundary decision, and why the inheritance flags are not the fix

The obvious repair is `(A;OICI;FA;;;{sid})` so children inherit the grant. It was
rejected. Control IPC's legitimate concern is one descriptor and one lock;
making its ACEs inheritable would hand it the ACL of the entire runtime tree
recursively — logs, model-hub, and every model file the Runtime ever downloads —
stripped to user and SYSTEM. It also carries a Windows-only hazard:
`_acl_signature` compares raw ACE bytes including the flags byte, and
`_validate_security_descriptor` demands an exact match, so putting `OICI` in the
shared SDDL risks failing validation on the files created with it, which would
break service startup on Windows only.

The descriptor moved to `runtime/control-ipc/endpoint.json` instead. The stamped
directory now contains only objects control IPC created, each with its own
explicit protected security descriptor, so stripping inherited ACEs from them is
a no-op. The lock follows automatically: `_descriptor_lock` derives it with
`descriptor_path.with_name(...)`, and the only `_ensure_private_directory` calls
take that same parent.

**The non-inheritable ACE is now correct by construction, not merely tolerated.**
Nothing inside that directory relies on inheriting anything, so adding `OI`/`CI`
would fix nothing and would only push control IPC's policy onto files it does not
own. Both `config/paths.py` and the SDDL itself carry that note, because the next
reader would otherwise "repair" it.

Cost of the move was one path expression. No Rust, TypeScript, workflow or
manifest computes that path, and there is no persisted-shape risk:
`core/control_ipc.py` is not an ancestor of master and appears in no `v*` tag,
only `gh-v3.1.1rc5`..`rc9`, this PR's own pre-releases. The descriptor is also
ephemeral state rewritten at every service start. On POSIX nothing changes at
all: `write_descriptor_atomic` is reached only from `WindowsLoopbackHost.publish`.

### The test, and the assertion that would have passed on the defect

The reproduction asserts that a file which exists **before** the securing call is
still readable **after** it. The natural phrasing — "a file created inside the
secured directory is readable by the process that created it" — describes the one
case that still works today, and would have gone green on broken code. That is
the same failure mode as asserting on the SDDL string, one level subtler.

Both tests reach the endpoint through
`paths.get_runtime_control_ipc_endpoint_path()` rather than a literal path, so
they state the required behaviour rather than the chosen remedy and would have
held against either candidate fix. They ride the existing `windows-control-ipc`
job, which already runs `tests/test_control_ipc.py`, so no workflow change was
needed.

The red run settled a fact neither the lane nor the orchestrator had
established. At `7981a1c9e`, job `107054477281` reported **1 failed, 31 passed**:
the pre-existing-sibling case died on `lock.open("a+")` with `[Errno 13]` —
the same call and errno that killed rc9 — while the create-path case **passed**.
So a child of a directory created by `CreateDirectoryW` with a protected,
non-inheritable descriptor takes the creating token's *default* DACL, which is
explicit rather than inherited, and propagation never touches it. Only children
that hold inherited ACEs can be orphaned. The create path is therefore a
regression guard, not a second instance of the defect.

## H16 — `vibe stop` hung because the process scan shelled out per process

`gh-v3.1.1rc10` got further on Windows than any candidate before it: the ACL fix
held, the service took the lock, the readiness poll passed, and `vibe start`
printed a valid receipt. Then `vibe stop` produced **not one byte on either
stream and never returned**, and the packaging probe killed it at its 60s
budget. `vibe_remote.log` has no shutdown entry, so the service was never asked
to exit — the hang was inside the stop command, before it signalled anything.

### Mechanism

`cmd_stop` prints nothing before `runtime.stop_service()`, and `stop_service`
calls `extra_service_process_pids()` unconditionally after resolving the lock
owner. That runs `service_processes()`, which walks **every process on the
machine**. Per process it did a liveness probe and then `_process_command_from_info`,
which fell through to `get_process_command(pid)` whenever psutil could not read
the command line — and `process_iter` fills a denied attribute with `None`,
which on Windows is the ordinary outcome for most system processes.

On Windows `get_process_command` spawns `powershell -NoProfile -Command
Get-CimInstance Win32_Process ...` with **no timeout**, then retries with `pwsh`
when the first returns nothing. Two process launches per unreadable process,
several hundred processes, on the platform where a process launch is most
expensive.

The defect is not Windows-only, only Windows-visible. The same fallback runs
`ps -p` once per process on macOS — the reproduction launched 64 of them for 64
scanned processes — and escapes notice only because a fork is cheap there.
Linux never reaches it, because `get_process_command` reads `/proc` first.

Nothing else on the stop path can account for the 60s. `stop_pid` does call
`get_process_command`, but every one of those calls sits on the POSIX branch:
on Windows it returns through `_terminate_process_windows` first. The remaining
Windows lookups are single-pid ones — owner resolution, UI identity — a handful
of calls, not one per process.

### The fix

`_process_command_from_info` now answers from what `process_iter` already
collected, or not at all. Nothing is lost. The scan is explicitly secondary to
the service lock: it exists to find lock-less daemons left by older lifecycle
bugs, and such a daemon is a process this install started, whose command line
psutil can read. A process whose command line we cannot read is by construction
not ours. The one case that could have made this wrong — a lock holder with an
unreadable command line — was never findable this way either, because
`service_lock_held_by` is only reached *after* the command filter passes; the
authoritative owner comes from `resolve_service_owner_pid` and the lock.
`_process_command_from_info` has exactly one caller, so the change cannot reach
anything else.

`_get_process_command_windows` keeps its shell, because naming one specific pid
is what it is for, but now runs it under a 5s timeout per shell. The existing
`except Exception: continue` already catches `TimeoutExpired` and moves on.

Rejected: making that lookup try psutil before the shell. psutil returns a
shlex-joined argv while the CIM query returns the raw Windows command line, and
`_command_looks_like_service_entry` and `_tool_family_from_text` parse those
strings — a quiet format change on an identity path, to save a little on calls
that are now bounded anyway.

### What the tests prove

`tests/test_runtime_process_scan.py` asserts the mechanism, not a duration: over
64 processes whose command line psutil cannot read, the scan must start **zero**
external processes. A timing assertion alone would be flaky and silent about the
cause; this one is red on every platform, so it keeps defending the invariant
without a Windows runner. A second test requires the single-pid Windows lookup
to pass a positive timeout. A third, Windows-only and read-only, runs the real
scan against the real process table under a budget priced off that machine's own
`process_iter` walk — `process_iter` never shells out, so the defect cannot
inflate the measurement and buy itself room. It rides the existing
`windows-control-ipc` job, which needs neither a packaged Runtime nor an
installer, so this class never has to cost a release candidate again.

## H17 — the endpoint budget was sized for a warm launch

rc11 installed on macOS and met the bootstrap error screen —
`runtime_discovery_failed`. Not Gatekeeper, not the config, not the descriptor:
`query_endpoint` gave `<private python> -I -m vibe desktop endpoint --json` 10s,
and a genuinely first launch cannot answer that fast.

Measured on an M-series Mac with the exact environment `RuntimeCommand::private`
builds:

| condition | wall time |
| --- | --- |
| cold — fresh extraction, fresh HOME | 15.81s |
| cold — fresh extraction, fresh HOME (independent repeat) | 18.36s |
| cold — x86_64 runtime under Rosetta | 19.99s |
| warm binaries, fresh HOME (config creation only) | 4.12s |
| warm binaries, warm HOME | 2.31s / 2.73s |
| warm binaries with `com.apple.quarantine` applied recursively | 3.26s, exit 0 |

The command is correct and always succeeds; it just could not finish inside 10s
the one time it mattered. `PrivateRuntimeBundle::prepare()` has only just
extracted the runtime tree, so nothing in it has been paged in or evaluated yet
and the interpreter pays for all of it on this one call. That cost is paid
**once per installed runtime version** — the second launch is warm and works,
which is exactly why our own DMG verification never caught it. Quarantine is not
a factor: these are ad-hoc/linker-signed binaries spawned directly rather than
through LaunchServices.

The asymmetry is what makes it a defect rather than a tuning question. Readiness
already budgets 120s (`DEFAULT_READY_TIMEOUT`, matching
`SERVICE_SLOW_START_TIMEOUT_SECONDS`); the endpoint query was the last step on
the cold path still holding a number sized for a warm one. Raised to 60s: 3x the
worst measurement for slower hardware, still well under readiness so discovery
cannot dominate a launch. The budget is only spent in full when the endpoint is
genuinely broken, and that failure is already retryable — so no adaptive tier,
no first-run flag, no persisted state.

The test drives the real `query_endpoint` deadline with a fake runtime that
sleeps past the old budget and then prints a valid descriptor, so it fails on the
constant rather than on a value read back from it. It is `#[cfg(unix)]`, like
every other process-spawning test in that module, which reuses the existing
`write_fake_runtime` harness instead of inventing a Windows equivalent for a
mechanism that has no platform-specific branch. **Ruled by the orchestrator:
accepted.** The 60s constant has no platform branch, so a Windows fake-runtime
harness would test the harness rather than the deadline.

## H18 — the rollback belonged to the region, not to the branch

Two reviewed heads, one root-cause class, which is what the review-loop circuit
breaker exists to catch:

| head | finding | shape |
| --- | --- | --- |
| `a09e74a2d` | roll back a newly started service when UI startup is **refused** | `start_ui` returns `None` |
| `5f6f47cdc` | roll back the service when UI startup **raises** | `start_ui` throws |

The invariant behind both is one sentence: *a service this command started must
not survive a start that never printed a receipt.* An unreceipted service is
adopted as `reused` on the next launch, so the desktop shell never acquires
scoped stop authority and Stop Runtime silently stops working.

The first fix encoded that invariant as a branch-local undo inside the single
failure mode the reviewer had named. That is why a second finding was not bad
luck: the invariant is a property of the whole pre-receipt region, and any region
guarded one named branch at a time yields findings one at a time forever. The
region runs from `start_service` returning through `print(receipt_line)`, and it
contains a UI restart, a process spawn, three status writes, a readiness wait
that can run to `SERVICE_SLOW_START_TIMEOUT_SECONDS`, the receipt builder and a
browser launch — every one of which can fail into the same orphan.

So the second fix does not add a second undo next to the first. One
`try/except BaseException` spans the region, the `ui_pid is None` branch loses
its local `stop_service()` and reaches the same mechanism by raising, and the
bare `raise` leaves the original failure unchanged. Exactly one stop per failed
start, none once the receipt is out, never against a reused service.

`BaseException` rather than `Exception` is deliberate. `KeyboardInterrupt` is not
an `Exception`, and the readiness wait is both the longest step in the region and
the likeliest moment for a user to give up — an interrupted start orphans the
service exactly like a crash does. `SystemExit` is included on the same logic:
nothing in the region exits on purpose, and to the next launch an exit before the
receipt is indistinguishable from any other start that never finished.

Four mutations, each mapped to one requirement: restoring the old branch-local
shape fails the three new region tests while still passing the two the first
review earned; `except Exception` fails only the interrupt test; dropping the
`not service_reused` scope fails both reused-service tests; and turning the guard
into a `finally` fails the receipt-boundary test.

**The lesson, generalised.** When a fix encodes an invariant, encode it for the
region that owns the invariant, not for the branch the reviewer happened to name.
A finding names an instance; the fix has to name the class, or the next instance
is already written.

## H19 — rc12: the private Runtime invalidated its own install

rc12 launched once, then paid a full 358MB re-extraction on every later launch,
and after two launches refused to start at all.

### Mechanism

`RuntimeCommand::private` runs the interpreter with `-I`, and `-I` implies `-E`:
every `PYTHON*` variable is ignored, including the `PYTHONDONTWRITEBYTECODE=1`
the launcher sets. The interpreter therefore wrote `__pycache__/*.pyc` into its
own install tree. `verify_installed_tree` checks that tree against the archive
manifest, so the next launch saw extra files, judged the copy invalid and
extracted a fresh one into the repair slot. That copy was then invalidated the
same way, and with both slots invalid `prepare()` returned
`ArchiveVerification`: a bricked install whose only exit was deleting a
directory by hand.

### The fix

- **`-B` beside `-I`.** A flag cannot be dropped by `-E`, so the launcher now
  passes `-I -B -m vibe`. The test runs the real interpreter prefix against a
  scratch tree and asserts no bytecode appears in it. Cost, measured on the
  endpoint command: 7.50s cold, 3.30s warm — inside H17's 60s budget. Shipping
  precompiled bytecode would remove that cost, but it is a packaging change and
  `desktop-package.yml` is frozen for this release.
- **Two invalid copies recover instead of stranding.** The old refusal existed
  because a daemon started from a copy that was valid at launch may still have
  its files open. `discard_install_path` keeps that safety: it renames the
  primary slot to `.discard-<pid>-<seq>` (atomic, and open handles survive it),
  then deletes the renamed copy best-effort, so the reinstall lands on a name
  nothing is reading. A tampered tree is still never executed. This is a
  deliberate contract change: `two_tampered_install_slots_fail_closed` is
  replaced by `two_tampered_install_slots_are_reinstalled_rather_than_stranded`,
  which repeats the double-tamper three times and asserts that exactly two slots
  and no discarded copies remain after each round.
- **A bootstrap log.** This takes up the deferred `stderr(Stdio::null())` ledger
  entry below. The shell appends to `bootstrap.log` in the application's local
  data directory (`~/Library/Application Support/bot.avibe.desktop/bootstrap.log`
  on macOS), capped at 64 KiB and trimmed at line boundaries from the front
  through a sibling file and a rename. It records one `runtime.prepare` line
  (`reused` / `extracted` / `failed`, duration, root or error) and one
  `endpoint.query` line per attempt (outcome, duration, exit status, error, and
  up to 4 KiB of stderr tail). `reused` exists for this line: an install that
  reports `extracted` on every launch is the rc12 defect in one word. Writing the
  log never changes an outcome — every I/O error in it is dropped.

**Known residual, accepted by the orchestrator.** `discard_install_path` renames
the primary slot away even while a Runtime of the same version is still running
from it, and that Runtime outlives the window. Files it already holds open
survive the rename; imports it has not yet made do not. With `-B` in place, both
slots can go invalid only through external tampering, and the alternative is the
permanent brick this section removed, so the trade stands.

### A timing fact the timeout test had to learn

The endpoint-timeout test passed alone and failed under the full `cargo test`
run. Instrumented, the first execution of a just-written executable on macOS
took ~4.85s to reach its first line under that load; later executions of the
same file took 10–25ms, and the spawn call itself returned in ~1.6ms. The test
now runs its fixture once before the measured call, so that one-time cost sits
outside the deadline by construction rather than inside a wider margin. The
fixture writes its stderr through `/bin/echo`, because `exec` would discard the
shell's own unflushed buffer.

## H20 — review of `5da131969`: three findings

- **F1 — rollback undid the service but not the UI.** This is the third reviewed
  head in H18's class, so the breaker applies and the fix names the invariant
  rather than the branch: *this invocation leaves running nothing it created.*
  Created means `pid is not None and not reused` in the `ProcessStartInfo` that
  `start_service` and `start_ui` already fill in, so no new state is needed. The
  region's `except BaseException` undoes in reverse order: the UI, then the
  service. `stop_ui` is called with `stop_remote_access=False`, because
  `vibe start` never brings a tunnel up — `remote_access.start()` is reached only
  from the UI's explicit endpoint and from `vibe remote` — so any tunnel alive at
  rollback predates the command, and stopping it would destroy a remote URL this
  invocation did not create. Consumers: an interrupt before the receipt with a UI
  this invocation started (both stopped, UI first, tunnel kept); the same failure
  with an adopted UI and a reused service (neither stopped); a receipt-builder
  failure (both stopped); the success path (neither stopped). Mutations: dropping
  the `ui_start.reused` scope fails only the adopted-UI test; removing the UI
  stop fails the interrupt and receipt-builder tests.
- **F2 — a missing private backend could upgrade an external CLI.**
  `resolve_cli_path` falls back to basename discovery on `PATH`, so when the
  configured desktop-managed path was missing, ownership was judged on whatever
  external binary discovery found, and the generic upgrade ran against it.
  Ownership is now judged on the configured path first and the discovered one
  second. A missing managed backend routes to `_run_desktop_backend_install`,
  and a missing private-runtime `codex` with no toolchain returns
  `desktop_managed_backend` naming the configured path. Tests put a real external
  CLI on `PATH` behind tripwires on every process-spawning seam. Mutation:
  judging ownership on the discovered path alone fails four cases.
- **F3 — `*` as the setup host.** `*` is a word, not an address; `getaddrinfo`
  rejects it. **Fixed here, pre-existing on `master`, not introduced by this
  PR:** uvicorn's bind on `master` fails the same way for the same input. It is
  now normalised once, in `_normalized_bind_host` (to `0.0.0.0`, next to the
  existing `[::1]` → `::1` unbracketing), which every listener derives from, and
  the separate `*` rule in `requires_desktop_loopback_listener` is gone.
  `_is_wildcard_setup_host` is unchanged. A test binds the `*` spelling for real.
  Mutation: removing the normalisation fails three tests, including a real
  `gaierror`.

Also fixed in this head: `test_start_ui_replaces_stale_live_pid_when_health_check_fails`
stubbed `ui_server_healthy`, but adoption on this branch is decided by
`_ui_server_compatible`, which probes the real listener. The test therefore
adopted whatever answered on `127.0.0.1:5123` — on a developer machine, their own
running UI — and failed. It fails the same way at `5da131969` without this
round's changes, and passes on CI only because nothing listens there. It now
stubs the seam that decides. The listener it found was the rc11 desktop app the
orchestrator had launched on the same machine to prove H17's root cause: the
"real services are production data" case in `AGENTS.md`, met in practice.

## H21 — review of `bb0c8de75`, native Settings, and what this head leaves out

Three review findings, fixed in their own commits, plus one owner item.

- **F1 — a spawned child could outlive the step that was meant to record it.**
  Every rollback could undo only what it had been told it created, and it was
  told only after the spawn primitive returned. The work between `Popen` and
  that return (the Memory UI secret written to stdin, the pid record) could
  raise with the child already running, and H18 and H20 each closed one such
  window at a consumer. The fix is at the primitive: `spawn_background` and
  `spawn_service_background_process` share one implementation that kills and
  reaps the child if anything after `Popen` fails, then re-raises, and the
  service start applies the same rule to its reservation write. `cmd_start`
  opens its rollback region before `start_service`, so an interrupt during the
  lock wait undoes a service it created. Consuming tests use real sleeping
  children with the failure injected at the pid write, the stdin write and the
  reservation. This head still returned the child before its caller captured
  it, and closed the parent's handles after the region; H22 closes both gaps.
- **F2 — the main window dropped every new browsing context.** `target="_blank"`
  links and `window.open(url)` did nothing, so the OAuth dialog's provider link
  could not be followed. The main window now answers each request with one
  decision: http(s) goes to the system browser and the request is denied;
  anything else, `about:blank` included, is denied. There is deliberately no
  relay webview: on macOS a popup webview must share the opener's
  `WKWebViewConfiguration`, and wry registers each webview's init scripts and IPC
  handler on that shared content controller, so a relay would copy its scripts
  into the main window and, when dropped, remove the main window's IPC handler.
  The window's config entry is `create: false`, so the startup window and every
  recreated one come from `ensure_main_window` and carry the handler.
  **Accepted UX limitations of this release:**
  - The OAuth dialog pre-opens `about:blank`, which is now denied, so it falls
    back to its visible provider link. That link needs one extra click; it opens
    in the system browser, and the callback still completes through loopback
    polling.
  - The Show Page launch menu hides "New link" in the shell, because that item
    navigates a pre-opened tab that never exists here. "New window" is kept.
  - Every other "open in new tab" affordance (Dock, Library, app search, Show
    Pages list) now opens the Workbench URL in the system browser instead of
    doing nothing.
- **F3 — the private Runtime inherited the shell's `PYTHON*` variables.** `-I`
  shields only the interpreter it is passed to; the Controller and UI
  interpreters the Runtime starts run without it, so an inherited `PYTHONHOME`
  aborted both and a `PYTHONPATH` imported code from outside the verified tree.
  The private `RuntimeCommand` now strips every inherited `PYTHON*` variable
  (case-insensitively on Windows) before its own overlay. The development shell
  keeps the user's environment.
- **The desktop marker.** The main window defines a non-writable
  `window.__AVIBE_DESKTOP_SHELL__ = true` through an initialization script before
  any Workbench script runs, top-level document only: macOS injects it into the
  main frame alone, and the script checks `window.self === window.top` because
  WebView2 injects into subframes too. The UI reads it through `isDesktopShell()`
  in `ui/src/lib/desktopShell.ts`, deliberately separate from `useIsDesktop`,
  which is the viewport breakpoint. No user agent sniffing and no
  `__TAURI_INTERNALS__` check.
- **Settings… in the app menu and the tray** (`CmdOrCtrl+,`). It delivers
  `avibe://settings` through the existing deep-link path, which maps it to
  `/admin/settings/service`; the Workbench redirects that legacy path to
  `/settings/service`. The item is enabled only while the shell monitors a
  Workbench it navigated to, and the handler applies the same rule before
  delivering, so the bootstrap window is never navigated to the Workbench origin
  and nothing is queued for a later start. No new Tauri command.

**Deferred by the orchestrator: Check for Updates.** The runtime's update checker
short-circuits for a managed runtime; PyPI cannot see the `gh-v` desktop builds;
and answering from GitHub Releases is a new resolver, not a small change. It
moves to a follow-up PR.

**Not in this head: the overlay title bar.** The owner's shape (macOS overlay
title bar, hidden title, a shell-only top inset under the traffic lights, a drag
region on the top strip) needs a drag path, and on macOS there is only one.
WKWebView honours no `app-region` CSS and swallows the mouse events that would
move the window natively (tao notes the same about
`movable_by_window_background`); wry enables non-client regions only on
WebView2. Tauri's `data-tauri-drag-region` script invokes
`plugin:window|start_dragging` over IPC, and the Workbench is a remote origin
with no capability, so the call is refused. Making it work means adding a
capability with a `remote` URL for the loopback Workbench origin, which would
break this shell's stated invariant that no capability names a remote URL. The
lane will not change that boundary on its own authority. Shipping the overlay
without a drag region would leave an undraggable window, so the item was dropped
from this head, as the owner allowed.

## H22 — orchestrator hold on `f654f245b`

Two more boundaries in F1's class, one Settings gap, and the title-bar ruling.

- **F1, the parent's handles.** The spawn primitive closed its copies of the
  child's stdin and log-sink handles in a `finally`, after the region that owns
  the child. A close that raised therefore propagated with a running child the
  caller never received. The closes now run inside the region, before the
  hand-over; the failure path closes whatever is left quietly and re-raises the
  original error. Tests fail each of the three closes with a real sleeping child.
  Mutations: moving the closes back to `finally` fails all three; closing loudly
  on the failure path fails the two secret-write tests.
- **F1, the capture.** H21 accepted a window between `spawn_background`
  returning and `start_ui` calling `start_info.capture(pid, reused=False)`: a
  created UI that `cmd_start`'s rollback could not see. The service had the same
  window between its reservation and `result(pid, reused=False)`. The primitive
  now takes a `hand_over` callback and runs it inside the region, so nothing is
  returned before it is captured. For the UI it writes the pid record, then
  captures; for the service it registers the process, writes the reservation,
  then captures. A failure or interrupt at any step kills and reaps the child.
  A `withdraw` callback then removes the record, but only when the child was
  reaped and the record still names it, so a child that could not be confirmed
  dead stays findable by `vibe stop`. The existing `ProcessStartInfo` is the only
  ownership state; reused and adopted paths are unchanged; no signal masking.
  Tests inject a `KeyboardInterrupt` after the record or reservation and before
  the capture, with real sleeping children, both at the primitive and through
  `cmd_start`: no child survives, no record is left, no receipt is printed and
  the status never reads `running`. A third `cmd_start` test interrupts after a
  real UI was captured and proves rollback stops it through its record, before
  the service. Mutations: capturing after the return fails the UI and service
  tests at both layers; no `withdraw` fails four; withdrawing unconditionally
  fails the confirmed-dead test; skipping the UI capture fails four, including
  the real rollback; skipping the kill fails eleven.
- **Settings at the Workbench hand-off.** The item's availability was
  recomputed only on tray status refreshes, so after the shell navigated to the
  Workbench it stayed disabled until the monitor's first tick. The main window's
  page-load `Finished` handler now refreshes every native control through the
  same `refresh_native_controls` rule, passing no status, so the status already
  shown is kept and a bootstrap page still enables nothing. Mutations: dropping
  the page-load refresh fails the boundary test; a page load that invents a
  status, or ignores the one shown, fails the unit test.
- **Title bar.** The orchestrator's ruling: this TEST release keeps the stock
  native title bar, and no capability is widened. Removing it is a follow-up,
  scoped independently of Check for Updates.

## H23 — Start at Login terminal policy on `1f869b519`

- **Windows checkout.** `1f869b519` makes `shell_boundaries.rs` normalize CRLF
  to LF once, in `shipping_text`, before any source assertion. A Windows
  checkout had failed a multi-line check. Test-only; a CRLF copy of `lib.rs`
  must read as the same shipping text.
- **The finding.** Codex P2 4086509105 found the uninstall's restore closure
  discarding the result of `enable()`. When the disable succeeds, the removal
  fails and the re-enable also fails, Start at Login stays off with nothing
  reported, and the menu can show a stale check.
- **Circuit breaker.** This was the third head in one root-cause class:
  compensation for a failed multi-step operation is incomplete (reviewed heads
  `d65847228`, `b85261e41` and `1f869b519`). The orchestrator diagnosed the
  class and ruled that the chain ends in a terminal policy, not a deeper
  compensation.
- **The ruling.** Every Start at Login write that must settle the menu goes
  through one policy, `settle_start_at_login`:
  1. write the requested state once;
  2. read it back;
  3. draw the checkbox from what was read, even on error;
  4. report a failed write, a failed read, or a read that disagrees with the
     request using the existing `login_failure` message.

  The write is an `FnOnce`, so nothing is retried and nothing is persisted.
- **Where the policy is used.** The menu toggle and the uninstall's restore
  both reach it through `set_start_at_login`. The restore callback now returns
  whether the registration came back. A failed restore ends as
  `UninstallOutcome::KeptWithoutLogin`: the login failure is reported, the
  existing runtime-removal failure is still reported, and neither the success
  dialog nor the exit runs.
- **Not covered by the policy.** The pre-removal disable stays outside it. Its
  failure already fails the uninstall closed, with the existing report, before
  the Runtime is touched.
- **Tightening.** Counting a disagreeing read as unsettled narrows the toggle.
  Before, it reported only errors; now a write that claims success but reads
  back the other state is reported too.
- **Tests.** Unit tests drive the policy directly, and the uninstall tests
  drive the restore through it. A new case: disable succeeds, removal fails,
  restore is attempted and fails. That run draws the observed state, reports
  both failures and never reaches `Removed`. A boundary test pins the one
  adapter, one call to the policy, one `.enable()` and one checkbox write.
- **Mutations.** Each mutation below fails at least one test:
  - ignoring the restore's result;
  - dropping the failure report;
  - counting only errors as unsettled;
  - skipping the redraw on error;
  - restoring outside the helper;
  - retrying in the adapter.
- **Scope.** No new command, capability, dependency or copy. No UI or title-bar
  change. This commit touches only `desktop/src-tauri/src/lib.rs`,
  `desktop/src-tauri/tests/shell_boundaries.rs` and this plan.
- **Correction to the release-file freeze.** Earlier sections, from H4 on, say
  the release implementation files are byte-identical to
  `13082a8501ce5d1e771f56395a72fc062859cf8b`, apart from the two
  `release_ai.yml` checkout pins. Each claim held at the head it named. As a
  cumulative statement it has not held since `51784cc2b`, the merge of master
  `fb460f2bf` (#2120, Memory removal).
- **Where the release files stand.** Object hashes against `13082a850`:
  - Unchanged: `desktop/README.md`, `docs/desktop-test-installation.md`,
    `scripts/desktop_release.py` and `.github/workflows/desktop-package.yml`.
  - Changed: `.github/workflows/release_ai.yml`,
    `tests/test_desktop_release.py` and `tests/test_release_verification.py`.

  Every change beyond the pins comes from master and entered only through that
  merge: #2120's own removal of the Memory runtime job and its verifier tests,
  and the merge's conflict resolution adapting this branch's release tests to
  that removal. The resolution dropped the Memory assets and `needs` entries,
  and added a focused source-SHA assertion in place of a check #2120 removed.
  No commit after `51784cc2b` changes these three files, and this candidate
  does not either.

## Known-by-design ledger additions

- **Taken up in H19.** `query_endpoint` sets `stderr(Stdio::null())` and the
  shell keeps no log file, so an endpoint that fails on a user machine yields
  zero diagnostics — the reason H17 had to be reproduced offline before it could
  be explained. The bootstrap log now records the endpoint's stderr tail and
  whether each launch reused or extracted its Runtime.
- **Deferred.** `vibe start` logged *"Started UI pid=6116 but required health
  checks did not pass"* during the rc10 run at 05:51:35, yet the packaging
  probe's own readiness poll passed moments later and the start receipt reported
  `outcome=started`. The UI health wait is tighter than a cold Windows runner's
  real startup, so the warning is noise on a start that in fact succeeded.
  Recorded, not chased: widening that wait is a separate change with its own
  evidence to gather.
- **Deferred.** `clamp_window_frame` picks the single largest-overlap monitor and
  clamps unconditionally, so a saved frame that legitimately spans two adjacent
  displays is moved on restore. Window geometry only, off the packaging path, and
  recoverable by moving the window. Tracked in issue 2139.
- **Deferred.** `BackendLifecycleChip` returns the desktop-managed hint before it
  reaches the error branch, so a failing desktop-managed backend reads as
  healthy in the popover text. The error badge and Reinstall action still render,
  so the failure stays visible while the wording is wrong. Tracked in issue 2140.
- **Fixed here.** This PR gave `runtime.start_ui` a `None` return for a stale UI
  it could not stop, and left `cmd_start` unaware of it: the command ran on to a
  receipt `validate_start_receipt` rejects, so the service it had just started
  stayed alive with no receipt ever printed, and an unreceipted service is
  adopted as `reused` on the next attempt. `cmd_start` now rolls back the
  service *it* started and fails there. Consequence class is #2135's, but the
  `None` return is ours, so the one caller this PR broke is fixed here and
  nothing in receipt or adoption semantics is touched. A start that fails this
  way still leaves the status file reading `starting`, exactly as the adjacent
  `start_service` failure path already does; correcting that is a separate,
  pre-existing concern and was deliberately left out rather than half-fixed.
- **Known flake, not chased.** `test_lifecycle_status_report_does_not_block_stop`
  asserts a `< 0.5s` wall-clock bound and failed once under a loaded local run,
  then passed alone 5/5 and on the full re-run. This PR does not touch it.
- **Known flake, not chased.** CI run 35844911380 at `bb0c8de75` failed only
  `tests/test_reply_enhancer_platform.py::test_file_link_parser_handles_many_openers_in_bounded_time`:
  12.84s against its 10.0s wall-clock bound. This PR does not touch
  `core/reply_enhancer.py` or that test, and it passes locally at about 4.6s.
  Classified by the orchestrator as runner timing, not a finding.
