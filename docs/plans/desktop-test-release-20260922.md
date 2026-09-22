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

## Remaining gates and handoff

- This lane delivers only a local commit on `feat/desktop-test-release-20260922`
  from `5589edc71292921281f95d072916628a9790fa6a`. No push, PR, tag, dispatch,
  Release mutation, merge, deployment, or Watch changes were performed.
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
