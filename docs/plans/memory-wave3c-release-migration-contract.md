# Memory Wave 3c Release And Migration Contract

> Status: proposed; implementation and merge require owner/orchestrator approval
>
> Baseline: `origin/dev` at `7eac90c65accc86bfe5d9bed0d06f065725d9406`
>
> Scope: Wave 3c Phase 0, Doc B; Doc A merged in PR #1742

## Decision

Wave 3c has one transition release: the first `avibe-os` release whose core
wheel no longer bundles the Memory implementation. That transition is a hard
dependency boundary, not a bridge or stepping-stone release. Its core metadata
requires the exact matched `avibe-memory` distribution even when Memory is
currently disabled, because pre-split planners cannot preserve an optional
package shape.

The existing release identity, one asset-complete Draft, and one finalizer
remain the publication owner. The finalizer stages and verifies both
distributions, publishes the asset-complete GitHub Release, and reverifies its
public assets before any package-index upload. It then publishes Memory to PyPI,
verifies its exact public resolution and hashes, and only then publishes core.
Memory-before-core ordering applies within PyPI; no second tag, finalizer, or
release identity is introduced.

Doc A merged in PR #1742. Doc B depends normatively on its lifecycle and
readiness invariants: `PackageLifecycleTransaction` owns mutation and recovery,
while this document defines release-family and migration rules that must produce
resolver-satisfiable targets. The focused post-merge lifecycle clarification in
PR #1746 is independent of these release-family decisions; gates 2b-4 wait for
both documents. No implementation begins from this Phase 0 draft.

## Background And Lineage

PR #1736 delivered Wave 3b's synchronous Memory-aware upgrade and existing
supervisor rollback. PR #1739 closed unmerged after five reviews, four heads,
and 15 threads. PR #1741 then combined lifecycle and release migration, closed
unmerged at `7516adc3a483d7eb54e60fda5d15149cb89f2a19`, and was split into Doc A
and this document. Doc A then merged as PR #1742 at
`7eac90c65accc86bfe5d9bed0d06f065725d9406`; its two post-merge advisory
clarifications are carried independently by PR #1746. The retained branches are
evidence only; no code is cherry-picked.

The release rules below reconcile the reviewed #1741 material with the owner
decisions for the first non-bundled hop, rollback families, and KBD retirement.

## Scope And Non-goals

This document owns:

- the first non-bundled `avibe-os` release and its exact hard Memory dependency;
- Memory distribution and runtime-manifest ownership;
- staged asset verification, Memory-before-core publication ordering, and
  transition-gate removal;
- legacy first-hop compatibility and one-time first-start shape reconciliation;
- release-family rollback rules that consume Doc A's exact captured shape; and
- retirement/reconciliation of `MEMORY-INDEP-018-KBD-1`, `KBD-5`, and `KBD-6`.

Non-goals:

- no bridge, compatibility stepping-stone, extra release identity, or second
  finalizer;
- no package mutation during startup;
- no second lock, lifecycle primitive, pending marker, UI acknowledgement,
  durable UI job, caller-local coordination, or process-topology change;
- no `PluginHost`, UDS/RPC, second Memory process, QR/Doctor/UI-reload
  coordination, or changes to Doc A's readiness/admission/UI invariants;
- no product, test, catalog, workflow, release, manifest, or config change in
  this Phase 0 PR; and
- no implementation before this document and the required lifecycle
  clarification receive owner approval; Doc A itself is already merged.

If a migration proposal needs a second finalizer, a second identity, or a
second coordination primitive, it is outside this contract and stops for an
owner decision.

## Invariant 1: Transition Dependency And First Hop

The transition release has an exact `Requires-Dist: avibe-memory==<core
release>` hard dependency in its base `avibe-os` metadata. The dependency is
present regardless of `memory_required`; it is the compatibility guarantee for
old planners, not a startup install instruction. The core wheel contains no
Memory implementation or EverOS runtime manifest. Memory implementation,
artifact, and manifest files ship only in `avibe-memory`.

There is no bridge release and no stepping-stone release. The path is:

1. A pre-split release upgrades to the one transition release using the old
   planner and old supervisor semantics; no Wave 3c transaction is assumed.
2. The transition release's hard dependency installs the exact Memory wheel as
   an ordinary package-manager dependency before the old release can start the
   new core.
3. Once the transition installation is present, its first start performs a
   one-time read-only reconciliation of the observed core/Memory shape against
   the transition family. It records and projects a warning through the
   existing state/audit surface when the shape is legacy, residual, or
   mismatched; it never installs, uninstalls, rewrites package metadata, or
   changes the captured shape during startup.
4. Subsequent Wave 3c mutations use Doc A's transaction owner and exact
   pre-mutation capture.

The transition first-start observation is keyed by the transition release and
stored in the existing `state_meta` migration-marker surface. The marker is
an observation/warning record only, is written at most once after the read-only
scan, and is safe to retry if persistence fails. It is not a reservation,
intent, pending-restart protocol, or package transaction record.

`MEMORY-INDEP-022` reserves packaged evidence for this hop: an old bundled core
must start with any residual split `avibe-memory` distribution present without
import shadowing, the transition first start must emit one shape-reconciliation
warning without package mutation, and rollback must reproduce the captured
bundled-plus-residual distribution shape exactly.

## Invariant 2: Rollback Family Semantics

Doc A requires every constructed rollback plan to be resolver-satisfiable. Doc B
supplies release-family rules; it may reject an inconsistent captured shape but
may not create an unpinned fallback.

| Captured family | Rollback target and cleanup |
| --- | --- |
| Pre-split bundled core | Stage exact legacy core, stop the failed generation, explicitly uninstall the replacement `avibe-os` and every canonical `avibe-memory` provider introduced by the forward mutation, install the staged legacy core, then verify replacement metadata/provider absence and canonical Memory-provider absence |
| Pre-split bundled core plus residual split Memory | Capture the residual canonical provider, exact Memory version, and cardinality; stage the exact legacy core and residual Memory artifact, remove the failed replacement shape, install the legacy core, and delete/reinstall Memory as needed to reproduce the captured residual provider/version/cardinality exactly |
| Optional-era split core plus Memory | Install the captured exact core pin and independently captured exact Memory pin, then verify provider cardinality and versions |
| Optional-era split core without Memory | Install the captured exact core pin, explicitly remove any Memory introduced by forward mutation, and verify zero canonical providers |
| Transition core with exact matching Memory | Stage and restore both captured exact distributions, then verify both versions and exactly one canonical Memory provider |
| Transition core with missing Memory | Fail closed before mutation; the hard dependency target cannot be reconstructed safely |
| Transition core with mismatched Memory | Fail closed before mutation; the captured shape is resolver-inconsistent with the hard dependency |

Restoration and readiness are orthogonal. `restored` means the installed
shape exactly equals the `ResolvedRollbackPlan` target in distribution presence,
exact versions, and canonical provider cardinality; it does not require the
restored release to project `ready`. An optional-era mismatched core/Memory shape
can therefore be restored exactly while readiness remains non-ready and
repairable. A transition missing or mismatched Memory is different: its exact
hard dependency makes that captured family resolver-inconsistent, so admission
fails closed before mutation and no rollback plan is constructed.

Legacy rollback is a cleanup operation, not omission of a requirement. It
re-enumerates every canonical `avibe-memory` dist-info provider and requires a
cardinality of zero for the bundled target or exactly one matching provider for
a split or bundled-plus-residual target. A residual provider present in the
captured bundled shape is package state, not cleanup residue: rollback restores
its exact captured version and cardinality and never removes it as a side effect.
Residual cleanup is a separate explicit repair intent. Duplicate providers,
missing/unreadable metadata, a missing staged artifact, failed replacement-core
uninstall, or failed post-cleanup verification all fail closed into Doc A's
recovery/quarantine path.

The rejected alternative is to accept any core/Memory mismatch and let a broad
resolver choose a convenient pair. That hides residue and can make a
transition target impossible to prove. Explicit family rejection is simpler,
preserves exact pre-mutation evidence, and keeps every constructed plan
executable. The optional-era independent pins remain available because they
express a real captured shape, including a mismatch that can be restored exactly
but remains non-ready; the hard-dependency transition family does not.

`MEMORY-INDEP-019` therefore includes duplicate-provider rejection, legacy
replacement-core uninstall and absence verification, exact bundled-residual
restoration, healthy transition restoration, transition missing or mismatched
rejection, and exact split-family rollback.

## Invariant 3: Publication, Manifest Ownership, And Gate Removal

The existing release workflow stages one asset-complete GitHub Draft and has one
final publication owner. The Draft remains private through all staged checks,
then becomes the public asset source before either Python distribution is
available from PyPI. The finalizer performs these checks and ordering inside
that one release identity:

1. Build the Memory wheel/sdist and Memory-owned EverOS manifest/assets. Stage
   them in the Draft with hashes and distribution metadata; do not claim public
   availability.
2. Build the transition core artifacts without Memory implementation or
   EverOS-manifest files. Verify core hashes, metadata, and the complete staged
   asset set while the release remains Draft.
3. Verify every staged asset hash, filename, metadata version, manifest release
   identity, and local resolver closure for the exact transition dependency.
   For every staged sdist, build a wheel in an isolated environment and apply
   the exact ownership and dependency assertions used for its staged wheel.
   Before GitHub Release finalization, manifest verification uses staged bytes
   and hashes. Any staged or rebuilt-wheel failure prevents every public action.
4. Immediately before publication, repeat the complete staged and isolated
   rebuild checks. The single finalizer publishes the asset-complete GitHub
   Release and records its verifiable asset/hash checkpoint.
5. Re-download the public GitHub assets and require every manifest URL, archive,
   and hash to match staging. No PyPI upload begins until these public checks
   pass; a rerun skips only an exact already-published GitHub asset set.
6. Upload the Memory wheel and sdist to PyPI, record their version/hash
   checkpoint, then resolve and download the exact Memory distribution from the
   package index. Require its public bytes, metadata, and Memory-owned manifest
   hash to match staging. On rerun, the same finalizer skips an exact published
   Memory artifact and resumes at core; absence or non-identical bytes fail
   closed.
7. Upload the core wheel and sdist to PyPI, record their checkpoint, then
   resolve/download both public distributions together and recheck metadata,
   exact dependency closure, manifests, and hashes. Only after these checks pass
   may the finalizer declare the transition release available and remove the
   transition gate. Memory-before-core is enforced within PyPI.

No release manifest points at Draft/private assets or a differently-versioned
distribution. A failed staged or public check leaves the gate in place and
requires idempotent recovery through a rerun of the same finalizer and release
identity. Core archive assets may therefore be publicly downloadable from the
GitHub Release briefly before core is installable from PyPI; that archive
visibility is explicitly accepted and is not a package-index availability
claim. If the GitHub Release is public but a PyPI step fails, the same
idempotent finalizer reverifies the GitHub checkpoint and resumes from the
Memory or core PyPI checkpoint. If Memory is already on PyPI but core publication
cannot complete, the identical Memory artifact remains published by default, is
recorded as stranded in the release audit, and is not advertised as a completed
transition release. It is yanked only when the Memory artifact itself is
defective, never merely because core publication failed. Recovery does not
publish another Memory version, create a second finalizer, or mint a replacement
release identity.

The release guard scans both wheels and sdists plus the staged/public asset set.
It proves that the core wheel and sdist contain no Memory implementation or
EverOS manifest, while the Memory wheel and sdist contain the owned
implementation, manifest, and complete matching content/metadata. Direct sdist
inspection is necessary but not sufficient: before any public action, each
staged sdist is built into a wheel in an isolated environment and that wheel must
pass the exact ownership, dependency, content, and metadata assertions applied
to the staged wheel. A rebuild failure prevents GitHub Release finalization. The
guard also proves resolver compatibility at the staged and public gates,
same-finalizer continuation from exact GitHub/Memory checkpoints, and the
stranded-Memory keep/yank policy.

Scheduled manifest verification, backup, and recovery move with ownership. The
guard remains dual-form: pre-transition releases retain legacy discovery from
the `avibe-os` wheel, while transition and later releases discover the
Memory-owned artifact and extract the manifest from `avibe-memory`. Missing,
ambiguous, or unreadable transition-family Memory artifacts fail visibly rather
than being classified as a manifest-free legacy release. This workflow migration
is Gate 5 implementation evidence and must be complete before the transition is
published.

## Invariant 4: KBD Retirement And Compatibility

The transition contract retires the three known-by-design residuals without
silently changing old-release behavior:

- `MEMORY-INDEP-018-KBD-1`: the old stopped pre-split planner can no longer
  drop enabled Memory at the first non-bundled hop because the transition core
  hard-depends on the exact Memory release. The first-hop packaged matrix is
  `MEMORY-INDEP-022`.
- `MEMORY-INDEP-018-KBD-5`: legacy bundled rollback now stages the exact old
  core, uninstalls replacement core and all split Memory providers first, and
  verifies absence before declaring restoration. Resolver-inconsistent shapes
  fail closed under `MEMORY-INDEP-019`.
- `MEMORY-INDEP-018-KBD-6`: transition and later split captures include exact
  Memory provider cardinality/version, so a uv-to-pip or installer transition
  cannot silently erase an installed-but-disabled package from rollback shape.

The one-time first-start warning is observational and does not bridge old and
new transaction semantics. Pre-split releases retain their old supervisor
behavior; the package transaction begins only with the transition release and
Doc A's owner approval.

## Scenario And Evidence Matrix

This matrix is the sole definition owner for `MEMORY-INDEP-022` and
`MEMORY-INDEP-023`; the main isolation plan only references these IDs and must
not duplicate or redefine their contracts.

| Scenario | Contract | Required automated evidence | Packaged/release evidence |
| --- | --- | --- | --- |
| `MEMORY-INDEP-018` | Doc A UI recovery and exact package lifecycle remain valid across release-family transitions | Reference Doc A's nonce/identity polling and terminal/active/quarantine truth table | Settings repair, enabled upgrade, restart/transport loss, rollback, and post-release availability using real wheels |
| `MEMORY-INDEP-019` | Every family rollback is exact and resolver-satisfiable | Provider cardinality property; legacy replacement-core uninstall/absence; bundled-plus-residual and healthy-transition exact plans; transition missing/mismatch matrix; independent split pins | Core-only, bundled residual, matching split, healthy transition, optional-era mismatch, duplicate provider, legacy cleanup, resolver failure, and activation rollback wheelhouse |
| `MEMORY-INDEP-020` | Doc A's single transaction owner remains the only mutation/admission primitive | Legacy `restart_status.json` fixture; nonce recovery/unknown ID; quarantine and ordinary-restart busy cases; no extra release coordination | Concurrent package requests, ordinary restart contention, killed-owner recovery, and release finalizer observations |
| `MEMORY-INDEP-021` | Not-required packaged Memory remains import-free per Doc A | Disabled/safe-degraded/whole-config import guard | Core-only and transition first-start smoke with no optional implementation import |
| `MEMORY-INDEP-022` | Pre-split first hop is inert with residual split package; transition first start reconciles shape once; rollback preserves the exact residual shape | Fixture for bundled old core plus residual split `avibe-memory`; capture version/cardinality; import-shadow guard; one-time warning marker/read-only assertion; exact bundled-plus-residual rollback equality | Old bundled wheel starts normally; transition wheel hard dependency resolves exact Memory; first start emits warning and performs zero package mutation; failed hop restores the exact legacy core and residual Memory shape |
| `MEMORY-INDEP-023` | Healthy transition shape restores exactly; missing or mismatched Memory is rejected before mutation | Matching-transition plan stages/restores both exact distributions and provider cardinality; truth table for missing distribution, unreadable metadata, duplicate provider, and mismatched version keeps mutation call count zero | Failed post-transition upgrade restores exact matching core/Memory; packaged transition with each invalid shape fails closed before package mutation and preserves release gate |

`MEMORY-INDEP-020` release evidence additionally covers a legacy
`restart_status.json` fixture, expired intent/nonce recovery, kill-owner plus
hung-child recovery, and an ordinary restart that returns `busy` while a live
package reservation is held. The release guard adds staged-asset verification,
mandatory isolated sdist rebuild equivalence, dual-form manifest discovery, and
package-index Memory-before-core ordering evidence.

## Migration And Compatibility Sequence

1. Keep pre-split releases unchanged and accept their released config, package,
   and `restart_status.json` shapes as read-only inputs.
2. Finalize and reverify the asset-complete GitHub Release, then publish the
   matched Memory distribution to PyPI. Do not make a package-index
   core-availability claim before Memory's staged, public-asset, and PyPI checks
   pass.
3. Ship the transition core with the exact hard dependency and no Memory
   implementation. Its first-start reconciliation only observes and warns.
4. After the transition release is available, enable Doc A's transaction owner
   for forward mutation, exact capture, recovery, and ordinary restart
   exclusion. There is no bridge period with mixed owners.
5. Remove the transition gate only after the packaged matrices 022/023, Doc A's
   018-021 evidence, and staged/public finalizer checks are green.
6. Later releases may relax the base hard dependency only under a new owner
   decision and new migration evidence; this document's gate is not silently
   generalized.

Old persisted records are never rejected solely because they predate the
transition. Unknown newer transaction schema remains Doc A's fail-closed
recovery-only state. The first-start observation marker is additive, warning-only
state and can be absent without blocking startup.

## Recovery Inventory From Retained Branches

| Retained behavior | Reconciliation |
| --- | --- |
| #1739 loader-owned probe and structured unsafe-rollback mapping | Owned by Doc A; release rules consume the resulting readiness and fail-closed error semantics |
| #1739 packaged Settings repair and `MEMORY-INDEP-018` evidence | Re-run under Doc A's transaction identity and release-family matrix |
| #1741 frozen execution bundle and child deadline | Required by Doc A; release finalizer stages matching artifacts and does not replace the bundle owner |
| #1741 single-finalizer staged publication ordering | Preserved and sharpened here with mandatory sdist rebuilds, public GitHub assets before PyPI, package-index Memory-before-core ordering, and resumable checkpoints |
| #1741 legacy cleanup and KBD-1/5/6 notes | Converted into explicit transition, family, and scenario rules above |

No retained branch is an implementation base. This document and Doc A are
specifications only; product changes require separate owner-approved PRs.

## Phase Gates

1. **Phase 0, Doc A:** PR #1742 is merged. Gate 2a loader
   readiness/probe work and `MEMORY-INDEP-021` are unlocked under a separate
   owner-approved implementation lane; this docs run does not start it.
2. **Phase 0, lifecycle clarification:** PR #1746 is independent of Doc B and
   must receive owner certification and merge before gates 2b-4 open.
3. **Phase 0, Doc B:** PR #1747 is merged; this focused post-merge
   clarification must also receive owner certification and merge before Gate 5.
4. **Implementation gates 2b-4:** rollback types/019, lifecycle transaction/020,
   and UI recovery/018 require both Doc B and PR #1746 to merge.
5. **Gate 5, release transition:** this clarification must be merged and the
   scheduled guard's dual-form legacy/Memory-owned discovery, backup, and
   recovery must be implemented and verified. Only then, after the transition
   wheelhouse, scenario 022/023, Doc A's 018-021 evidence, isolated sdist rebuild
   equivalence, and the single finalizer's staged/public-GitHub/package-index
   checks pass, may the release gate be removed.

No Phase 1 product code begins from this draft. Any finding that challenges the
no-bridge transition, single finalizer, Memory-before-core ordering, or
fail-closed transition family is a design decision for the owner, not an
editorial correction.
