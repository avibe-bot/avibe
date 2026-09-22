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
