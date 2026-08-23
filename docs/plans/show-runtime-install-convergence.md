# Show Runtime Install Convergence Contract

Status: proposed migration contract

Scope: analysis and design only; no production implementation

Code baseline: `903414cc4` (`origin/master` when this branch was created)

Assumption: the GitHub source provider is removed before this migration starts

## Decision

Show Runtime must stop owning a second managed-dependency installer.
Within the four-dependency boundary audited here (git, Memory, model-hub, and
Show), `core/managed_runtime.py` becomes the sole owner of manifest acquisition,
platform selection, archive acquisition and verification, staging, install
identity, install metadata, current-pointer persistence, mutation locking,
retention, and install-state inspection. A small Show-specific adapter remains
responsible for four product concepts that the other three dependencies do not
have:

1. policy/install/serving availability and operation-outcome publication;
2. the system Node command and `minimum_node` prerequisite;
3. the operator-supplied direct archive escape hatch; and
4. the npm provider escape hatch.

The adapter should be a composed `ShowRuntimeInstaller` based on
`ManagedRuntimeManager`, held by `ShowRuntimeManager`. Making the serving
manager itself inherit the installer would preserve the current mixed owner and
would not create a clean deletion boundary.

The migration is not a rewrite of the serving half. Request proxying, process
lifecycle, readiness, prewarm, WebSocket routing, context capability, and stop
remain in `core/show_runtime.py`.

This is deliberately not a whole-repository sole-owner claim.
`core/tmux_runtime.py` is a live 699-line fifth managed-dependency installer. It
independently owns six of the seven common capabilities counted below (all but
retention/cleanup), while also carrying tmux-specific macOS signing, runnable
probing, utf8proc, and terminfo requirements. The supplied 24-pair and
1,621-line audits do not cover it, so silently adding tmux would make the size
and migration estimates fictitious. This contract accounts for tmux as a
remaining owner but does not migrate it. A separate audit must measure its
released layouts and product seams before it can become another
`ManagedRuntimeSpec` consumer.

## Contract Boundaries

The shared layer must own these invariants for every dependency in this
convergence boundary:

- An install identity is the identity of the selected platform artifact, not
  the digest of unrelated manifest entries. Its minimum fields are runtime
  version, selected archive platform, and selected archive SHA-256.
- An installed identity is read from the install metadata and current pointer.
  It is never manufactured from the currently selected manifest.
- A durable operation claim is a released persistence contract, not an alias
  for the current in-memory identity type. Model-hub `install-state.json`
  schema versions 1 and 2 must be dual-read and normalized: a legacy target
  that differs only by `manifest_sha256` still names the same platform artifact.
  A new target shape requires a schema-version bump and safe degradation.
- Status inspection is local and non-mutating. Failure to inspect is unknown or
  error, never proof of absence.
- A released on-disk layout is dual-read indefinitely. Migration may write a
  canonical new shape, but it must adopt released Show layouts without a
  download and without rewriting them merely to make status work.
- Archive bytes are content-addressed by verified SHA-256. Cleanup protects the
  current install and every retained rollback install, and fails closed when
  that protected set cannot be established.
- Automatic cleanup runs after the install transaction commits. Its failure
  means delete nothing and publish or log a separate cleanup report; it never
  changes install success, the current pointer, or a durable claim settlement.
- A real cleanup and an install use one mutation guard for the whole operation.
  A dry-run never creates the guard and holds a read-only exclusion through the
  complete plan.
- Lock paths are never followed through symlinks, hard links, or Windows
  reparse points; the locked descriptor must still be the live path.
- The default extraction policy remains regular files and directories only.
  Internal archive links require an explicit per-runtime opt-in.
- Default behavior for git, Memory, and model-hub remains binary-strict:
  `binary_sha256` and `bin_path` stay required. Show alone may use an
  archive-verified directory artifact invoked through Node.
- New Show manifest installs persist the selected `minimum_node` beside the
  artifact identity. A released record without it reports Node compatibility as
  unknown, never supported. To preserve offline reuse, it may enter the existing
  bounded readiness probe; only a successful probe admits serving for that
  process lifetime, and a failed probe does not relabel the install as absent.

`ManagedRuntimeSpec` needs only declarative differences, not callbacks for every
step. The named seams are:

- configurable persisted provider id and metadata filename;
- strict binary artifact versus archive-verified directory artifact;
- optional internal-link extraction policy, defaulting to denied;
- a staged-entrypoint verifier/command builder for directory artifacts;
- manifest-extension validation and persisted extension metadata
  (`minimum_node` for Show);
- legacy install candidates and source-lineage admission;
- staging-name patterns for preview and cleanup; and
- a result adapter that maps shared reasons into the Show availability model.

The shared manager continues to expose binary paths to its three existing
subclasses. Show's adapter converts its verified entrypoint into
`[...node_command, cli_path]`; the shared layer does not learn about Node.

## Per-Function Verdict And Drift Ledger

Legend:

- **Subset**: delete Show's implementation and call the shared implementation.
- **Superset**: adopt the shared implementation and add the named general seam.
- **Different**: a Show-owned wrapper/hook remains because of the stated product
  requirement; common mechanics still move underneath it.
- **Shared defect** and **Show defect** are latent defects in the side that lacks
  the behavior. **Show requirement** is an intentional product difference.

| Pair | Verdict and target seam | Behavior present on only one side |
| --- | --- | --- |
| `status` | **Different.** Keep a Show projection over a shared local `InstalledArtifactSnapshot`. Requirement: Show must report policy, install, and serving dimensions plus its Node prerequisite and non-manifest providers. | **Show requirement:** explicit-command/provider dispatch, Node fields, and the three-dimensional availability payload. **Shared defect:** shared status requires the selected manifest before it will resolve an install, and publishes the selected manifest version beside the installed boolean; git, Memory, and model-hub can therefore report a good persisted install as missing or pair it with an identity that is not on disk. **Show defect:** the manifest status branch may fetch a remote manifest and may raise; the dependencies API then fabricates `install.state=absent` (issue #1658), converting failed inspection into absence. |
| `probe_archive_reachability` | **Superset.** Delegate manifest archive probing to shared code and retain a provider-dispatch wrapper for direct archive and npm. Reason translation belongs in the Show result adapter. | **Show requirement:** direct local archive/path probing and `runtime_*` reason vocabulary. The shared `probe_url` already handles `file:` correctly. No existing shared consumer needs Show's provider dispatch. |
| `_clean_locked` | **Superset.** Shared cleanup gains an archive-retention planner and a legacy-candidate/source-lineage seam; Show supplies its released layouts and staging patterns as data. | **Show requirement:** released two-level and three-level install layouts, packaged-versus-custom manifest lineages, and `prebuilt-*`/`manifest-*` staging. **Shared defect:** it has no archive protection or reclamation and no post-install retention, so git, Memory, and model-hub accumulate versioned downloads and installs. **Show defect:** install-directory discovery still uses `Path.glob`/`is_dir`, which can suppress inspection errors that the shared `_rglob_install_metadata` deliberately preserves. |
| `_preview_busy_reason` | **Superset.** Keep the shared in-process lock hold, add a typed `busy` versus `guard_unavailable` result, and accept staging patterns from the spec. | **Shared defect:** an uninspectable lock, an inode mismatch, and real contention all become `*_install_already_running`, so its three consumers publish the wrong cause. **Show defect:** when the lock path was absent, Show does not hold its in-process `RLock` through planning; it relies on a later race check and can inspect a tree while another local thread mutates it. |
| `_windows_preview_busy_reason` | **Superset.** Move Show's Windows identity/reparse hardening and typed result into the shared preview guard; staging names remain declarative. | **Shared defect:** the shared side does not distinguish an unopenable/replaced guard from contention and does not use the Windows reparse-point test used by Show. **Show requirement:** only the concrete staging names differ. |
| `_resolve_manifest_archive` | **Superset.** Shared resolution gains a content-addressed cache-key policy, per-call offline override, and source provenance in its result. | **Shared defect:** caching under `archive.name` and never reclaiming old names causes unbounded versioned archive growth in git, Memory, and model-hub. **Show requirement:** source provenance feeds Show's configured-versus-packaged recovery classification. **Show defect:** the cache fast path tests `exists()` rather than `is_file()`, so a directory or special entry at the digest path can escape the normal cache-miss path as an inspection exception. |
| `_safe_extract_tar` / `safe_extract_tar` | **Superset.** Add an opt-in internal-link extraction policy; retain the shared regular-file/directory-only default. | **Show requirement:** the Node bundle contains internal symlinks, including npm package and `.bin` links, and must reject only links that escape the extraction root. Git, Memory, and model-hub must not inherit this relaxation. Whether valid internal hard links are actually required is an explicit unknown described below. |
| `_runtime_platform_tag` / `runtime_platform_tag` | **Subset.** Delete the Show helper. | No behavioral difference; the bodies are identical apart from the name. |
| `clean` | **Superset.** The shared method owns locking and cleanup; a thin Show wrapper maps the shared report to the existing CLI/Doctor archive payload. | **Shared defect:** shared cleanup omits downloaded archives entirely, so its three consumers have no archive counts, protection result, or reclamation outcome and their caches grow without bound. **Show requirement:** CLI and Doctor retain their existing Show-specific payload wording. **Show defect:** real cleanup deletes staging and version directories before `_clean_downloaded_archives` acquires the install guard, while shared cleanup holds its mutation lock around the whole real operation. |
| `_write_manifest_install_metadata` | **Different.** Keep a Show metadata compatibility hook, but make the shared atomic writer persist its output and the selected `minimum_node`. Requirement: Show is a directory graph invoked through system Node and must continue reading released `.vibe-show-runtime.json` records whose provider is `manifest-cache` and which have no leaf-binary or Node-requirement fields. | **Show requirement:** legacy filename/provider/field shape and archive-only integrity. **Shared behavior:** `runtime_id`, `bin_path`, and `binary_sha256` support strict single binaries. **Show defect:** direct `Path.write_text` is not atomic, and omitting `minimum_node` makes offline Node compatibility unknowable; a torn or incomplete metadata write can turn a completed install into an uninspectable or compatibility-ambiguous one. |
| `_preview_lock_probe` | **Superset.** Shared probe gains typed failure and the reparse-point predicate. | **Shared defect:** special/uninspectable guard paths are mislabeled as contention, and Windows reparse points are not tested explicitly. |
| `_preview_guard` | **Subset.** Delete the Show context manager and use the shared one. | No behavior unique to Show. **Show defect:** when no descriptor exists, Show's guard does not retain the in-process lock through planning; the shared lifecycle does. This is the same root cause recorded for `_preview_busy_reason`, not a second finding. |
| `_guard_path_matches_fd` | **Superset.** Use Show's cross-platform exclusive-regular-file predicate in shared code. | **Shared defect:** it checks POSIX symlink/regular/link-count fields but not Windows reparse attributes, so the three shared consumers have a weaker path-identity boundary on Windows. |
| `_release_preview_guard` | **Subset.** Delete Show's release routine with its preview implementation. | No behavior unique to Show. **Show defect:** Show has no in-process lock to release on the absent-descriptor path because it failed to retain one; shared releases both resources. This is the release-side manifestation of the same preview-lifecycle defect. |
| `_manifest_status_payload` | **Superset.** Shared status gets a persisted manifest-extension map; Show contributes `minimum_node` and available platforms. | **Show requirement:** Node compatibility and the complete Show platform set are user-visible diagnostic facts. **Show defect:** released metadata does not preserve `minimum_node`, so disk-only status cannot distinguish supported from unsupported Node. `source_url`, `loaded_from`, and `release_state` on the shared side belong to manifests that declare them and are not missing Show behavior. |
| `_manifest_install_dir` | **Superset.** Change the shared default identity to runtime version + selected platform + selected archive SHA-256, and use a legacy-candidate seam to adopt old paths. | **Shared defect:** the whole-manifest digest makes an edit to another platform create a new install identity and force a download for git, Memory, and model-hub too. Show already has the correct platform-artifact identity and previous-fingerprint adoption. |
| `_archive_status_payload` | **Subset.** Use the shared projection after making leaf-binary fields optional and omitted for directory artifacts. | Show has only the common archive fields. The shared `binary_sha256`/`bin_path` fields are valid for strict binary artifacts, not missing Show requirements. |
| `_preview_raced_busy` | **Superset.** Shared race detection accepts staging patterns and checks them before trusting an absent lock path. | **Shared defect:** on a lockless/Windows or replaced-lock path, a fresh staging directory is evidence of a possible live install that its preview does not examine. |
| `_downloaded_archive_matches` | **Subset.** Delete and use shared archive size/SHA verification, then translate its reason. | No semantic difference beyond helper names and reason prefixes. |
| `_preview_lock_missing` | **Subset.** Delete and use shared. | No semantic difference beyond the guard-path attribute name. |
| `_manifest_archive_for_platform` | **Subset.** Delete and use shared. | Shared additionally supports declared platform aliases. Show's release manifest enumerates exact host tags, so the missing alias fallback is not a product requirement or a defect. |
| `_file_sha256` / `file_sha256` | **Subset.** Delete the Show helper. | No behavioral difference. |
| `_env_flag_enabled` / `env_flag_enabled` | **Subset.** Standardize on the shared explicit truthy set. | **Show defect:** every value other than `0/false/no/off`, including typos, currently enables offline or install-skip behavior. Repository callers and install scripts document/use `1` or `true`; an unrecognized value must not silently change network or install policy. |
| `_safe_path_part` / `safe_path_part` | **Subset.** Use shared for new paths; discover old Show paths by metadata rather than by reproducing the old sanitizer. | **Show defect:** `.` and `..` survive unchanged and invalid characters map to a different, weaker layout. Released manifests use safe commit/platform strings, while legacy custom paths must be adopted through the compatibility scanner. |

## Shared-Layer Bugs Exposed By The Drift

These are live shared-layer defects, not Show migration prerequisites disguised
as refactors. No issues should be filed until the owner chooses which to split
out.

1. **Manifest-dependent installed state (git, Memory, model-hub).**
   `ManagedRuntimeManager.status()` calls `_load_manifest(allow_network=False)`
   and only calls `resolve_binary()` when that selected manifest and archive are
   available. A missing package manifest, unavailable remote-manifest cache, or
   malformed override therefore publishes `installed=False` without inspecting
   the persisted `current.json` install. Memory has its own active-pointer
   resolver, but `super().status()` still gates its installed field on the
   manifest.
2. **Selected identity reported as installed identity (git, Memory, model-hub).**
   Shared status publishes `version=manifest.runtime_version`; the dependency
   row can pair that selected identity with disk state. The installed version
   must come from admitted install metadata, with the selected version reported
   separately as an update target.
3. **Whole-manifest install identity (git, Memory, model-hub).**
   `_manifest_install_dir()` hashes `manifest.digest:archive.sha256`. Editing an
   archive for any other platform changes the local path and causes a needless
   reinstall even though this host's selected bytes are identical.
4. **Whole-manifest durable claim identity (model-hub).** Released
   `install-state.json` schema versions 1 and 2 require an exact target containing
   `manifest_sha256`, and recovery passes that target back as
   `expected_target`. An unrelated-platform manifest edit can therefore turn a
   valid in-flight claim into `install_target_changed` even when every selected
   artifact field is unchanged.
5. **Unsafe mutation-lock path (git, Memory, model-hub).**
   `_acquire_mutation_lock()` delegates to `MigrationFileLock`, whose append
   open follows a symlink and can truncate a hard-linked or replaced file. It
   does not perform Show's no-follow, exclusive-regular-file, and post-lock
   descriptor/path identity checks.
6. **False contention diagnosis and weaker Windows preview (git, Memory,
   model-hub).** Uninspectable/special/replaced lock paths are reported as an
   active install, and Windows reparse attributes are not part of the shared
   predicate. This hides state corruption or a permission problem behind retry
   advice.
7. **Unbounded downloaded-archive retention (git, Memory, model-hub).** The
   shared cache uses versioned archive names and `clean()` never considers
   `downloads/`. Each release with a new name leaves its verified archive
   indefinitely.
8. **No post-success version retention (git, Memory, model-hub).** Shared
   `ensure()` never invokes its existing version cleanup. Git is reachable from
   `vibe runtime clean`, but Memory and model-hub have no equivalent user path,
   so old install directories have no automatic lifecycle owner.

The first six are independently testable correctness/security wins. The last
two should land with the shared archive-cache capability because their
protected-set contract is one unit.

## Uncounterparted Install Code

This section keeps the supplied 1,621-line grouping so implementation can be
scheduled against the original audit. Some groups split across destinations;
the classification is by behavior, not by today's function boundary.
“All four” below means the audited git, Memory, model-hub, and Show boundary;
tmux is accounted separately above and receives none of these capabilities in
this sequence.

### Belongs In The Shared Layer

| Supplied group | Lines | Shared capability and beneficiaries |
| --- | ---: | --- |
| Manifest handling | 421 | Manifest source caching, composite-artifact parsing, platform-artifact identity, local installed-state snapshots, atomic metadata/current writes, and legacy-candidate adoption belong in shared code. Show supplies only extension data and legacy admission. **All four** gain the corrected identity and disk-only inspection; only Show uses the composite artifact and released-layout hooks initially. Duplicate Show loading, matching, verification, and pointer writing becomes dead after cutover. |
| Archive cache / cleanup / protection | 355 | **Expectation confirmed, with one boundary correction.** Content-addressed storage, protected-set construction, error-preserving traversal, secure scan/unlink, abandoned-claim recovery, byte/count reporting, dry-run parity, and post-success pruning are generic. **All four** gain bounded archive storage. CLI/Doctor wording stays outside this layer; it is a Show consumer of the generic report. |
| Install lock and concurrency guard | 221 | One re-entrant in-process/cross-process mutation guard, safe path opening, typed contention/unavailable results, read-only preview, and race detection belong in shared code. **All four** gain it. Show contributes staging patterns only. The duplicate second `except OSError` in `_install_guard_locked` is dead. |
| Install orchestration | 195 | The manifest transaction and its target/result must be shared. The existing `expected_target` and `on_resolved` seams remain for model-hub claims. Show's admission policy and outcome publication do not move. **Show** gains deletion; the other three retain their existing call contract. |
| Status reporting | 89 | A local `InstalledArtifactSnapshot` carrying admitted installed identity, selected identity, comparison state (`matches`, `differs`, `not_comparable`, `unknown`), path, and inspection error belongs in shared code. **All four** gain truthful status. Show keeps only its product projection. |
| Archive handling | 120 (partial) | Safe extraction, content-addressed materialization, and staged-install verification use shared primitives. **Show** gains reuse. The unpinned direct archive source itself remains Show-specific. |
| Other | 18 (partial) | URL redaction, dependency-error construction, hashing, platform tagging, env parsing, and path sanitizing already exist in shared modules; Show wrappers are dead. |

### Genuinely Show-Specific

| Supplied group | Lines | Requirement |
| --- | ---: | --- |
| Install orchestration | 195 (partial) | Show has an automatic-versus-explicit policy, a forced-replacement operation outcome distinct from installed state, an explicit command, multiple providers, and a policy/install/serving availability model. The adapter owns those decisions and calls one shared manifest installer. |
| Archive handling | 120 (partial) | `VIBE_SHOW_RUNTIME_ARCHIVE_PATH`/`_URL` is an operator escape hatch with no pinned manifest checksum. Turning unverified input into a normal shared manifest would weaken the shared install contract. Keep source selection in Show while reusing shared extraction/staging helpers after the bytes are acquired. |
| Status reporting | 89 (partial) | Show projects the shared install snapshot together with Node availability and serving state. It must not make the shared manager understand sidecar health. |
| Node version checking | 61 | The Show artifact is a Node dependency graph and the manifest declares `minimum_node`; git, Memory, and model-hub ship their own executable and must not inherit a system-Node prerequisite. |
| npm provider | 37 | npm is a Show operator/source-ladder escape hatch. It remains outside the manifest installer and must converge only at Show's admission/result boundary. |
| Manifest handling | 421 (small compatibility portion) | The released `.vibe-show-runtime.json` provider/name/field shape and configured manifest source-lineage rule are persisted Show contracts. They remain as declarative legacy admission in the adapter, not as a second loader or writer. |
| Other | 18 (partial) | Show archive naming/default URL and reason-to-availability mapping remain local product data. |

### Dead After Preconditions

| Supplied group | Lines | Deletion condition |
| --- | ---: | --- |
| GitHub provider | 104 | Assumed removed before this migration. No target seam or compatibility path is designed for it. |
| Manifest handling | 421 (duplicate portion) | Delete Show manifest acquisition/parsing, archive selection, current-pointer writing, install matching, and current-manifest verification after shared composite artifacts and dual-read legacy admission pass. |
| Install lock and concurrency guard | 221 (duplicate portion) | Delete the Show `RLock` depth, preview implementation, and file-lock implementation after every real mutation and dry-run routes through the shared guard. |
| Install orchestration | 195 (duplicate portion) | Delete the Show manifest install transaction and manifest branch plumbing after the adapter consumes the shared result. |
| Archive cache / cleanup / protection | 355 (duplicate portion) | Delete the Show implementation after the generic report is projected unchanged by CLI and Doctor tests. |
| Other | 18 (duplicate portion) | Delete exact utility wrappers as soon as imports use their shared owners. |

The archive-cache verdict is therefore stronger than “copy the 355 lines into
the base class.” The capability is shared, but its current implementation must
be folded into the shared traversal, mutation guard, metadata schema, and reason
vocabulary. Keeping the block intact as a Show mixin would create a third owner.

## Migration Sequence And Stop Points

Every step is independently shippable. Do not begin the next step until the
acceptance property for the current step holds.

### Step 1: Make Shared Identity And Status Truthful

Change the shared platform-artifact identity, add dual-read adoption for its old
manifest-digest directories, and introduce the local installed-artifact
snapshot. Treat Model Hub's released durable claim target as a separate
versioned shape: dual-read schema versions 1 and 2, normalize their five fields
to the four platform-artifact identity fields for comparison, and write a new
claim shape only under a bumped schema version. Keep current public payloads as
projections during this step.

**Stop point:** git, Memory, and model-hub use the new snapshot and identity;
Show is untouched. Model-hub recovery can resume every released claim shape
without treating a manifest-only digest change as a target change.

**Acceptance test:** for each existing subclass, install one artifact, edit only
another platform's manifest entry, make the manifest source unavailable, and
assert that the original installed identity/path is still reported from disk
without archive access. Seed every released install layout and every released
Model Hub `install-state.json` schema, then assert adoption or claim resumption
without a write or download when the selected artifact identity is unchanged.

**What breaks:** expected functional breakage is **none**. Without dual-read
adoption, all three can appear missing or redownload while offline; that failure
blocks the step. Without claim normalization, an in-flight Model Hub operation
can fail as `install_target_changed`; that also blocks the step. Downstream
status consumers that currently call the selected version “installed” must be
updated in the same step.

### Step 2: Converge The Mutation Guard

Merge Show's no-follow/path-identity hardening and typed guard result into the
shared re-entrant mutation lock. Use it for the whole real cleanup and keep
dry-runs non-mutating.

**Stop point:** both current shared cleanup and ensure use the hardened guard;
Show still uses its own guard.

**Acceptance test:** for each shared subclass, prove same-process contention,
foreign-process contention, absent-lock preview, inode replacement, symlink,
hard link, Windows reparse simulation, and uninspectable path. A preview must
either return a complete stable plan or a typed non-success; it never creates
`.install.lock`.

**What breaks:** no installed runtime. A new
`<runtime>_install_guard_unavailable` reason is an intentional API correction;
any consumer that only recognizes `<runtime>_install_already_running` must be
updated before this stop point. Loosening `MigrationFileLock` globally is out of
scope; the managed-runtime guard must open and validate its own descriptor.

### Step 3: Add Composite Artifacts Without Weakening Binary Artifacts

Add the declarative metadata/provider filename, directory-artifact verifier,
manifest-extension validator and persistence map, and internal-link extraction
policy. Defaults remain exactly the current strict binary contract.

**Stop point:** a fixture directory artifact can install through
`ManagedRuntimeManager`; Show is not yet cut over.

**Acceptance test:** the fixture accepts an archive-verified directory with a
Node-style entrypoint and safe internal symlink, rejects an escaping link, and
persists its declared runtime prerequisite atomically. Existing git, Memory,
and model-hub fixtures still reject a missing `binary_sha256`, missing
`bin_path`, or any link member.

**What breaks:** expected breakage is **none**. If optional leaf checksums or
link acceptance become the default, integrity for all three existing
dependencies breaks and the step must not ship.

### Step 4: Add Shared Content-Addressed Retention

Materialize new downloads under their verified digest, dual-read the old
archive-name cache, build the protected set from every retained admitted
install, and add generic dry-run/cleanup reports. Enable post-success cleanup
one runtime spec at a time. Automatic cleanup is a post-commit maintenance
operation: an inspection or deletion failure produces its own report and skips
unsafe deletion, but cannot change the successful ensure result or durable
claim settlement.

**Stop point:** the capability is enabled for git, Memory, and model-hub before
Show depends on it.

**Acceptance test:** seed one install of every supported persisted metadata
shape plus current, rollback, stale, recent, temporary, symlink, abandoned
claim, and unreadable cases. Dry-run and real cleanup select the same eligible
set; current/rollback bytes are unchanged; unreadable protection metadata makes
the cleanup operation fail closed. Inject that failure after a committed
install and assert that the current pointer and successful result remain intact,
Model Hub settles its claim as successful, and the separate cleanup report
retains the inspection error. Re-run and assert idempotence.

**What breaks:** no runnable dependency. Stale unprotected archives and installs
are deleted by design. If any current or retained install lacks an admitted
archive identity, cleanup must skip rather than delete; a deletion in that case
breaks git, Memory, or model-hub rollback safety and blocks rollout for that
spec. Propagating an automatic-cleanup error through `ensure()` breaks an
already-committed install and Model Hub claim state, and also blocks rollout.

### Step 5: Cut Over Show's Manifest Provider

Create the composed Show installer spec/adapter. Route manifest prepare,
installed resolution, status facts, probe, lock, cleanup, extraction, metadata,
and current pointer through shared code. Keep direct archive, npm, admission,
availability, and serving behavior in Show. Dual-read every released Show
metadata and fingerprint layout. New metadata persists `minimum_node`; old
records without it produce an explicit unknown compatibility state and must
pass the bounded runtime readiness probe before serving is admitted.

**Stop point:** manifest-backed Show behavior has one installer owner, but the
old functions may remain unreachable for one comparison PR.

**Acceptance test:** run the existing Show matrix for packaged/local/remote
manifest, offline reuse, failed and forced replacement, unrelated-platform
manifest edits, previous fingerprints, legacy parent layout, configured source
lineage, internal symlinks, Node missing/unsupported, archive cleanup, Doctor,
and dependencies status. Add an invariant test that an injected status
inspection failure remains unknown/error and is never projected as absent. With
the manifest unavailable, a new metadata record plus downgraded Node must remain
unsupported, while every released record lacking `minimum_node` must remain an
installed artifact with unknown compatibility and cannot become serving-ready
without a successful bounded readiness probe.

**What breaks:** git, Memory, and model-hub have **no new behavior** in this
step. Show would break for already-installed/offline users if any released
metadata/provider/layout is omitted, and would break for normal archives if
safe internal symlinks are not enabled only for its spec.

### Step 6: Delete The Second Installer

Delete unreachable Show manifest installer, cache, lock, cleanup, and exact
utility functions. Reduce retained same-name methods to product wrappers; they
must not contain a second manifest transaction or filesystem policy.

**Stop point:** final architecture. Direct archive and npm are visibly separate
Show providers; the Show manifest provider has exactly one implementation.
tmux remains the explicitly accounted separate installer described in the
scope boundary.

**Acceptance test:** an AST ownership test rejects Show definitions or call
paths for shared manifest acquisition, archive verification/cache, install
identity, metadata/current writes, mutation lock, archive retention, platform
tagging, hashing, or extraction. The assertion is scoped to Show and must not be
described as a whole-repository singleton check. Run focused suites for all four
audited managers and the Show CLI/Doctor/dependencies consumers.

**What breaks:** no dependency behavior. Tests that monkeypatch deleted Show
private methods must move to the shared manager or public result boundary; that
is test migration, not retained compatibility. Do not preserve private shims
solely for those tests.

## Measured Size Estimate

Measurements use AST function spans (`end_lineno - lineno + 1`) at
`903414cc4`; blank lines outside functions, imports, dataclass declarations,
serving-only functions, and tests are excluded. These estimates cover only the
four audited dependencies. The separate 699-line tmux installer is measured as
an excluded owner, not included in deletion, movement, or shared-growth totals.

- Current install roots reach **110 functions / 2,579 lines**.
- Eleven GitHub-only reachable helpers account for **212 lines**. Excluding
  that already-owned removal leaves **99 functions / 2,367 lines**; a few
  GitHub dispatch lines inside mixed functions make this a conservative
  baseline.
- The 24 pairs occupy **561 Show lines** at this head. The supplied 540-line
  audit predates growth in `status` (97 to 108) and
  `_resolve_manifest_archive` (33 to 43).
- The current spans conservatively assigned to irreducible Show behavior total
  **961 lines** before wrapper reduction. Therefore **1,406 lines is the hard
  lower bound** on Show deletion even if every retained wrapper stays its
  current size.
- Shared delegation removes another measured 150-240 lines from the mixed
  status/probe/manifest-dispatch/cleanup wrappers. The implementation estimate
  is therefore **1,560-1,650 lines deleted from `core/show_runtime.py`**, not
  counting the GitHub lane.
- **732 source lines move semantically** to the shared owner: 384 for archive
  cache/reporting, 160 for hardened guard behavior, 155 for disk-only and
  legacy install admission, and 33 for internal-link extraction plus
  platform-artifact identity. These lines overlap the deletion figure; “moved”
  describes behavior, not a promise to preserve their text.
- Reusing the existing shared traversal, preview, metadata, pointer, and ensure
  code should require **480-560 net new production lines in
  `core/managed_runtime.py`**. The budget is 260-310 for archive retention,
  40-70 for guard hardening, 90-120 for disk/legacy admission, and 90-110 for
  composite-artifact/spec/result plumbing.
- Net production reduction across the two modules is therefore approximately
  **1,000-1,170 lines**.

The exact endpoint cannot be known without choosing the staged-entrypoint result
shape. The experiment that narrows the range is a compile-only Step 3 fixture:
implement the smallest directory-artifact subclass without touching Show, then
remeasure which Show wrappers can become direct projections. Do not use the
upper estimate as permission to introduce a general callback framework.

### Concept Count

The scoped count is derived from behavioral owners, not filenames. Within the
four-dependency audit, seven capabilities currently have two owners:
primitives/extraction, manifest selection, archive acquisition/cache, install
transaction/identity/persistence, mutation/preview locking,
retention/cleanup, and status/probe. Four Show-only concepts sit beside them:
admission/availability, Node prerequisite/command, direct archive, and npm.

| Audited state | Shared/common owner instances | Show-only concepts | Total |
| --- | ---: | ---: | ---: |
| Before | 7 capabilities x 2 owners = 14 | 4 | **18** |
| After | 7 capabilities x 1 owner = 7 | 4 | **11** |

Whole-repository accounting adds tmux as a third owner for six common
capabilities; it has no retention/cleanup implementation to count. On the same
method, the repository moves from **24** owner/concept instances before this
migration (18 scoped + 6 tmux) to **17** afterward (11 scoped + 6 tmux), not to
11 globally. Migrating tmux in a separately measured follow-up is required to
remove those final six duplicate owners.

The target adds seams and data fields, not another engine concept. A “generic
archive-cache helper” owned outside `ManagedRuntimeManager` would add an owner
and is therefore rejected unless it has an independent non-runtime consumer.

## Named Unknown

The repository proves that Show archives require safe internal symlinks: the
install test constructs both the package link and the npm `.bin` link. It does
not prove that internal **hard links** occur in shipped artifacts; no current
Show archive is present in this checkout. Before choosing that policy, download
one current release archive for each of the six declared platforms and enumerate
tar member types and link targets. If none contains hard links, keep them denied.
If one does, add a separate confined-hard-link test and opt-in flag rather than
treating symlink permission as permission for both.

## Non-Goals

- No change to Show serving or request behavior.
- No tmux migration. Its live duplicate ownership is explicitly counted above;
  a follow-up must first measure its persisted layouts, macOS signing, runnable
  probe, utf8proc, and terminfo seams rather than extrapolate Show's estimates.
- No resurrection or compatibility path for the removed GitHub provider.
- No attempt to force direct archive or npm into the manifest security model.
- No deletion of released on-disk compatibility readers.
- No issue filing as part of this design PR.
- No dependency addition.
