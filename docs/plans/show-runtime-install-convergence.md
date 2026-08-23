# Show Runtime Install Convergence Contract

Status: proposed migration contract

Scope: analysis and design only; no production implementation

Code baseline: measured at `903414cc4` and revalidated against
`2c6fcbe88` (`origin/master` on 2026-08-23). The intervening Show changes are
serving-side WebSocket invalidation and failure classification only; no install
function or measurement below changed.

Assumption: the GitHub source provider is removed before this migration starts

## Decision

Show Runtime and tmux must stop owning parallel managed-dependency installers.
Across the five-dependency family (git, Memory, model-hub, Show, and tmux),
`core/managed_runtime.py` becomes the sole owner of manifest acquisition,
platform selection, archive acquisition and verification, staging, install
identity, install metadata, current-pointer persistence, mutation locking,
retention, and install-state inspection. A small Show-specific adapter remains
responsible for four product concepts that the other dependencies do not have:

1. policy/install/serving availability and operation-outcome publication;
2. the system Node command and `minimum_node` prerequisite;
3. the operator-supplied direct archive escape hatch; and
4. the npm provider escape hatch.

Memory also retains one product concept outside the shared manifest engine: the
explicit `AVIBE_MEMORY_DEV_RUNTIME` development provider. It is selected and
validated before managed-state inspection and therefore remains an adapter
branch, not another shared provider or installer owner.

The adapter should be a composed `ShowRuntimeInstaller` based on
`ManagedRuntimeManager`, held by `ShowRuntimeManager`. Making the serving
manager itself inherit the installer would preserve the current mixed owner and
would not create a clean deletion boundary.

The migration is not a rewrite of the serving half. Request proxying, process
lifecycle, readiness, prewarm, WebSocket routing, context capability, and stop
remain in `core/show_runtime.py`.

`core/tmux_runtime.py` is a live 699-line fifth managed-dependency installer. It
independently owns six of the seven common capabilities counted below (all but
retention/cleanup), while also carrying tmux-specific macOS signing, runnable
probing, utf8proc, and terminfo requirements. It is therefore part of this
contract, not a narrowed exception to the sole-owner invariant. Show migrates
first because its larger surface exercises every required shared capability;
tmux follows as the final, smaller adapter migration. The original 24-pair and
1,621-line audits remain Show-specific; tmux is measured separately below.

## Contract Boundaries

The shared layer must own these invariants for every dependency in this
convergence boundary:

- An install identity is the identity of the selected platform artifact, not
  the digest of unrelated manifest entries. Its minimum fields are runtime
  version, the archive's persisted platform label, and the selected archive
  SHA-256. Host admission compares that label through the spec's declarative
  host-to-artifact platform aliases; literal spelling equality is not required.
- An installed identity is read from the install metadata and current pointer.
  It is never manufactured from the currently selected manifest.
- A durable operation claim is a released persistence contract, not an alias
  for the current in-memory identity type. Model-hub `install-state.json`
  schema versions 1 and 2 must be dual-read and normalized: a legacy target
  that differs only by `manifest_sha256` still names the same platform artifact.
  A new target shape requires a schema-version bump and safe degradation.
- Status inspection and operational resolution are local and never rewrite
  installed state. A complete, successful traversal that finds no pointer,
  metadata, version directory, or other install evidence proves clean absence.
  Any present-but-partial, unreadable, malformed, unsafe, or raced state is
  `unknown` or `error`, never absence. A manifest outage cannot make
  `status()`, Git's `resolve_git_path()`, or model-hub's
  `resolve_engine_path()` forget an admitted installed artifact.
- Manifest source outcomes are typed. Configured offline mode means no network
  attempt; a failed attempted fetch is `manifest_unavailable`; a source that is
  absent is `manifest_missing`; bytes that were reached but fail parsing or
  validation are `manifest_invalid`; and a valid manifest without a host
  artifact is `platform_unsupported`. Status and resolution continue to use an
  admitted disk artifact in every case, but `ensure()`/repair must not report an
  invalid or missing manifest as successful reuse. Only configured offline mode
  and a transient fetch failure may deliberately keep an admitted disk artifact
  operational without manufacturing a selected target, and their diagnostics
  and network-attempt evidence remain distinct.
- An explicit non-manifest provider is selected before any managed-state
  snapshot. In particular, `AVIBE_MEMORY_DEV_RUNTIME` remains a Memory-owned
  development provider: status and resolvers may validate the configured
  interpreter and `ensure()` returns `changed=false` without loading a manifest
  or reading/writing managed metadata, pointers, claims, or derived state.
- Runtime-specific admission is a read-only projection over the installed
  snapshot, but "read-only" describes the installed artifact, metadata, and
  pointer, not all derived state. After a successful Memory re-admission, the
  shared layer must atomically attempt to cache that result under
  `<runtime_dir>/derived/admission/`, keyed by the verified artifact identity
  and admission revision. It never writes the artifact tree, `current.json`,
  or a released pointer. A cache-write failure falls back to a process-wide
  in-memory result, shared by fresh manager instances, and never changes
  status success; a changed identity makes the entry ineligible immediately.
  A cache hit is usable only after the pointer, metadata, and artifact identity
  have been cheaply reverified. Failed or raising probes remain `unknown` or
  `error` and are not persistently cached as admitted.
- Derived-cache persistence uses one shared confined atomic writer. Every
  existing parent and destination component is opened without following a
  symlink or Windows reparse point, an existing target must be a single-link
  regular file, and the live path must still name the validated descriptor at
  commit. A redirect, hard-link alias, identity race, or permission failure
  leaves every external target byte-for-byte unchanged and falls back to the
  process-wide in-memory cache. This writer does not acquire the install
  mutation guard and is not a second installed-state writer.
- A released on-disk layout is dual-read indefinitely. Migration may write a
  canonical new shape, but it must adopt released Show layouts without a
  download and without rewriting them merely to make status work.
- Archive bytes are content-addressed by verified SHA-256. Cleanup protects the
  current install and every retained rollback install, and fails closed when
  that protected set cannot be established.
- The downloads namespace is typed, not “everything under `downloads/`.” Remote
  manifest caches and unknown non-archive names are durable or unowned facts and
  are always retained; only recognized, verified, unprotected archive entries
  are cleanup candidates.
- Automatic cleanup runs after the install transaction commits. Its failure
  means delete nothing and publish or log a separate cleanup report; it never
  changes install success, the current pointer, or a durable claim settlement.
- A real cleanup and an install use one mutation guard for the whole operation.
  A dry-run never creates the guard and holds a read-only exclusion through the
  complete plan.
- Lock paths are never followed through symlinks, hard links, or Windows
  reparse points; the locked descriptor must still be the live path.
- Extraction explicitly whitelists `REGTYPE`, `AREGTYPE`, `DIRTYPE`,
  `SYMTYPE`, and `LNKTYPE`; device, FIFO, and character members are rejected.
  Runtime specs still default to regular files/directories only. A link-enabled
  spec must validate both `name` and `linkname` against the extraction root and
  extract sequentially in archive order, resolving a link only to content
  already extracted. Two-pass and parallel extraction are forbidden. Extraction
  filter support is capability-detected, never inferred from `sys.version_info`:
  attempt the repository's existing `extractall(..., filter="data")` call and
  use the manually guarded fallback only when the keyword is unavailable. The
  filtered and fallback paths must have the same archive acceptance and
  rejection set.
- Default behavior for git, Memory, and model-hub remains binary-strict:
  `binary_sha256` must be declared and a safe resolved `bin_path` must exist.
  The path may come from the archive entry or the already-declarative
  `ManagedRuntimeSpec.default_bin_path`; canonical metadata and pointers persist
  the resolved value. Show alone may use an archive-verified directory artifact
  invoked through Node.
- New Show manifest installs persist the selected `minimum_node` beside the
  artifact identity. A released record without it reports Node compatibility as
  unknown, never supported. To preserve offline reuse, it may enter the existing
  bounded readiness probe; only a successful probe admits serving for that
  process lifetime, and a failed probe does not relabel the install as absent.
- Tmux remains a strict binary artifact. The exact released schema 1 manifest
  fixture, metadata, and pointer omit `binary_sha256`; only that released shape
  is dual-read as a legacy archive-verified install and re-admitted through the
  existing runnable/version/platform-preparation checks. Canonical tmux uses
  schema 2 and requires `binary_sha256`; a new or modified schema 1 manifest
  without it is invalid. The source leaf digest is verified immediately after
  extraction and before preparation. Preparation may then change bytes (macOS
  ad-hoc codesigning does); canonical metadata therefore records a distinct
  post-preparation installed digest, and subsequent disk admission verifies
  that digest plus runnable/version requirements. Neither digest may stand for
  both phases.

`ManagedRuntimeSpec` needs only declarative differences, not callbacks for every
step. The named seams are:

- configurable persisted provider id and metadata filename;
- strict binary artifact versus archive-verified directory artifact;
- optional internal-link extraction policy, defaulting to denied;
- a staged-entrypoint verifier/command builder for directory artifacts;
- manifest-extension validation and persisted extension metadata
  (`minimum_node` for Show);
- a read-only installed-artifact admission projection (Memory supplies its
  compatibility probe and tmux supplies runnable/version admission), with an
  opt-in, identity-keyed derived-admission cache for expensive deterministic
  success results;
- a confined, no-follow atomic writer for derived admission cache entries;
- declarative host-to-artifact platform aliases used by manifest selection,
  installed-state admission, resolution, and durable-claim normalization;
- legacy install candidates and source-lineage admission;
- staging-name patterns for preview and cleanup;
- explicitly ordered staged-binary source-integrity, preparation, installed-byte
  fingerprinting, and admission phases, using the existing
  `_prepare_binary_for_manifest`, `_binary_matches_manifest`, and
  `_binary_version` hooks without conflating their digests; and
- result adapters that map shared reasons into Show and tmux payloads.

The shared manager continues to expose binary paths to git, Memory, model-hub,
and tmux. Show's adapter converts its verified entrypoint into
`[...node_command, cli_path]`; the shared layer does not learn about Node,
codesign, utf8proc, or terminfo.

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
| `_safe_extract_tar` / `safe_extract_tar` | **Superset.** Atomically replace the shared file/directory-only extractor with the explicit type whitelist, root-confined `name`/`linkname` validation, sequential archive-order extraction, and capability-detected filter described above; retain link denial as the default spec policy. | **Show requirement:** released Darwin/Linux bundles contain 16 symlinks and one esbuild hard link, so a Show-enabled spec must accept both confined link types. **Shared migration blocker, not a current vulnerability:** shared currently rejects the first link and is safe because of that restriction. Removing the restriction without adding `linkname` validation in the same change would make the sole owner weaker than both Show and tmux. **Shared defect:** unlike Show, shared uses a Python 3.12 version gate and skips the available `data` filter on Python 3.10.12+ and 3.11.4+. Git, Memory, and model-hub keep link members denied by spec but currently miss that backported filter protection. |
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
3. **Whole-manifest install and durable-claim identity (git, Memory,
   model-hub).**
   `_manifest_install_dir()` hashes `manifest.digest:archive.sha256`. Editing an
   archive for any other platform changes the local path and causes a needless
   reinstall even though this host's selected bytes are identical. Model-hub's
   released
   `install-state.json` schema versions 1 and 2 require an exact target containing
   `manifest_sha256`, and recovery passes that target back as
   `expected_target`. An unrelated-platform manifest edit can therefore turn a
   valid in-flight claim into `install_target_changed` even when every selected
   artifact field is unchanged.
4. **Unsafe mutation-lock path (git, Memory, model-hub).**
   `_acquire_mutation_lock()` delegates to `MigrationFileLock`, whose append
   open follows a symlink and can truncate a hard-linked or replaced file. It
   does not perform Show's no-follow, exclusive-regular-file, and post-lock
   descriptor/path identity checks.
5. **False contention diagnosis and weaker Windows preview (git, Memory,
   model-hub).** Uninspectable/special/replaced lock paths are reported as an
   active install, and Windows reparse attributes are not part of the shared
   predicate. This hides state corruption or a permission problem behind retry
   advice.
6. **Unbounded downloaded-archive retention (git, Memory, model-hub).** The
   shared cache uses versioned archive names and `clean()` never considers
   `downloads/`. Each release with a new name leaves its verified archive
   indefinitely.
7. **No post-success version retention (git, Memory, model-hub).** Shared
   `ensure()` never invokes its existing version cleanup. Git is reachable from
   `vibe runtime clean`, but Memory and model-hub have no equivalent user path,
   so old install directories have no automatic lifecycle owner.
8. **Link-capable sole-owner extraction (Show migration blocker).** Shared
   `safe_extract_tar()` rejects every non-file/non-directory member. That is
   safe today, and its three current consumers need no links, but it makes four
   of Show's six platform archives uninstallable. The latent defect is created
   only if Step 3 removes that restriction without atomically adding confined
   `linkname` validation and archive-order extraction. This is not an allegation
   of a vulnerability in the current shared implementation.
9. **Version-gated extraction filter (git, Memory, model-hub; also tmux).** On
   Python 3.10.12+ and 3.11.4+, the runtime provides the PEP 706 `data` filter,
   but shared tests `sys.version_info >= (3, 12)` and calls unfiltered
   `extractall()` when that test is false. Its file/directory whitelist and path
   checks still apply, so this is missed available protection rather than the
   link-escape vulnerability described in item 8. Tmux repeats the same version
   gate; Show already uses capability detection.

Items 1-7 and 9 are live defects in git, Memory, and model-hub, except that the
durable-claim consequence in item 3 is model-hub-only. Items 6 and 7 should land
with one protected-set contract. Item 9 also affects tmux. Item 8 is a hard
migration prerequisite whose relaxation and hardening must be one commit.

## Uncounterparted Install Code

This section keeps the supplied 1,621-line grouping so implementation can be
scheduled against the original audit. Some groups split across destinations;
the classification is by behavior, not by today's function boundary.
“All five” below means git, Memory, model-hub, Show, and tmux. Tmux receives the
capability only at the final migration step; that timing does not justify a
second implementation.

### Belongs In The Shared Layer

| Supplied group | Lines | Shared capability and beneficiaries |
| --- | ---: | --- |
| Manifest handling | 421 | Manifest source caching, composite-artifact parsing, platform-artifact identity, local installed-state snapshots, atomic metadata/current writes, and legacy-candidate adoption belong in shared code. Show supplies only extension data and legacy admission. **All five** gain the corrected identity and disk-only inspection; only Show uses the composite artifact hook. Duplicate Show loading, matching, verification, and pointer writing becomes dead after cutover. |
| Archive cache / cleanup / protection | 355 | **Expectation confirmed, with one boundary correction.** Content-addressed storage, protected-set construction, error-preserving traversal, secure scan/unlink, abandoned-claim recovery, byte/count reporting, dry-run parity, and post-success pruning are generic. **All five** gain bounded archive storage. CLI/Doctor wording stays outside this layer; it is a Show consumer of the generic report. |
| Install lock and concurrency guard | 221 | One re-entrant in-process/cross-process mutation guard, safe path opening, typed contention/unavailable results, read-only preview, and race detection belong in shared code. **All five** gain it. Show contributes staging patterns only. The duplicate second `except OSError` in `_install_guard_locked` is dead. |
| Install orchestration | 195 | The manifest transaction and its target/result must be shared. The existing `expected_target` and `on_resolved` seams remain for model-hub claims. Show's admission policy and outcome publication do not move. **Show and tmux** gain deletion; git, Memory, and model-hub retain their existing call contract. |
| Status reporting | 89 | A local `InstalledArtifactSnapshot` carrying admitted installed identity, selected identity, comparison state (`matches`, `differs`, `not_comparable`, `unknown`), path, admission projection, and inspection error belongs in shared code. Its opt-in derived-admission cache is shared state, not a Memory pointer extension. **All five** gain truthful status; Memory also avoids repeating its expensive released-shape probe across processes. Show and tmux keep product projections. |
| Archive handling | 120 (partial) | Safe extraction, content-addressed materialization, and staged-install verification use shared primitives. **Show and tmux** gain reuse. The unpinned direct archive source itself remains Show-specific. |
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

### Tmux Addendum

Tmux is outside the supplied 1,621-line grouping, so its separate measurement
must not be blended into those buckets. Its 699 lines contain 38 functions / 568
function-span lines. Twenty-three common installer functions account for 451
lines and belong in the shared layer. Eight tmux product helpers account for 92
lines: macOS quarantine/codesign preparation and runnable/exact-version
admission are genuine tmux requirements. Three released-layout helpers account
for 17 lines and become declarative compatibility data; four public wrappers
account for 8 lines and remain. The remaining 131 non-function lines are imports,
constants, dataclasses, and class structure, most of which disappear with the
duplicate `TmuxArchive`/`TmuxManifest` model. The duplicate transaction,
manifest loader, archive cache, extractor, metadata/current writers, hashing,
platform mapping, env parsing, and payload helpers are dead after Step 7.

## Persisted-Fact Census

Step 1 is a source-of-truth migration, not a `status()` refactor. Every fact
that a manifest or install-time probe previously supplied to a disk reader must
either be persisted at install time or have an explicit `unknown`/`error`
projection with a safe default. No reader may reconstruct an installed fact
from the currently selected manifest. This table is the normative completeness
check for the migration.

Every predicate in the table is schema-aware and keeps these distinctions:

1. a successfully inspected, evidence-free runtime is **absent**; missing data
   inside a present or uninspectable state is **unknown/error**;
2. a source not inspected, not reached, missing, reached-but-invalid, or valid
   but without a host artifact has a different reason and mutation policy;
3. canonical writes persist their required fields, while a released shape may
   use a safe value that its versioned spec already declares;
4. artifact and host platform labels compare through the same declarative alias
   map rather than by literal equality; and
5. admission not attempted, attempted and rejected, and attempted but errored
   are distinct from admitted; and
6. inspection never rewrites installed state, but may write a best-effort
   derived cache outside the artifact, metadata, pointer, and downloads trees;
   a write failure changes neither the observed result nor runtime health.

| Fact needed by a disk reader | Authority today | Persistence contract: today -> target | Missing or invalid meaning | Acceptance scenarios |
| --- | --- | --- | --- | --- |
| Provider and runtime ownership (`provider`, `runtime_id`) | spec + manifest branch | Shared metadata/pointer have both; released Show/tmux provider/layout contracts can safely derive their runtime id -> canonical metadata/pointer write both | A fully empty tree is absent. A present unknown shape is foreign/corrupt; do not resolve or delete it | C1, C8, C9, C15, C23 |
| Installed artifact identity (`runtime_version`, artifact `platform`, `archive_sha256`) | selected manifest; current path also includes whole-manifest digest | Fields exist in the wrong shared identity -> canonical metadata/pointer persist this artifact tuple; the artifact platform remains its manifest label and host admission uses spec aliases | Identity unknown/error; never substitute selected identity. An alias-equivalent label is admitted; a label outside the mapping is not | C1-C9, C14-C15 |
| Install location and entrypoint (`install_dir`, `bin_path` or Show CLI path) | pointer + selected archive/default | Shared/tmux pointers persist it and Show is implicit -> canonical pointer persists the resolved verifier/entrypoint; any accepted manifest may safely derive an omitted `bin_path` from its spec default | An unsafe or unresolvable path is invalid. Field omission alone is not invalid when the versioned spec supplies a safe default; manifest unavailability is unrelated | C1-C2, C7-C10, C15, C17, C19-C20 |
| Source leaf integrity (`binary_sha256`) | selected archive + extracted bytes | One digest currently spans source and installed bytes -> strict canonical manifests persist the declared source digest and verify it before any preparation | Missing is allowed only for an explicitly versioned released legacy shape. A mismatch is source corruption and preparation must not run | C1, C15, C23 |
| Installed-byte integrity (`installed_binary_sha256`) or archive-verified directory integrity mode | post-preparation staged artifact | Mixed/implicit today -> canonical metadata persists the post-preparation digest or explicit directory/legacy mode separately from the source leaf digest | Unknown installed integrity is not strict verification. Show uses its directory verifier; released tmux uses archive + runnable/version admission | C1, C8-C10, C15, C23 |
| Installed admission outcome/revision (manifest installability, platform preparation, runnable/version probe) | install transaction + runtime hook | Usually implicit; Memory alone is explicit -> canonical metadata/pointer persist a versioned admission result; a successful released-shape re-admission may persist only a separate identity-keyed derived cache | Not attempted is `unknown`; a false result is `rejected`; an exception is `error`. None is ready or persistently cached as admitted | C1-C2, C7-C8, C11, C15, C21-C23 |
| Whole-manifest digest and source lineage | manifest + install metadata | Present today -> retained only as legacy locator/provenance, outside artifact identity | Legacy locator/provenance only, never installed identity; missing lineage makes source comparison unknown | C4, C9-C10, C14-C15 |
| Selected/update facts (selected version/artifact, archive catalog, `release_state`, source URLs) | current or cached manifest | Remote cache only -> remain selected facts and are never copied into installed identity | Configured offline is `not_attempted_offline`; a failed fetch is `manifest_unavailable`; missing source is `manifest_missing`; invalid reached bytes are `manifest_invalid`; no host artifact is `platform_unsupported`. Installed/admitted state is unchanged | C2-C6, C16-C17, C19-C20 |
| Show `minimum_node` | manifest | **Absent** in released metadata -> required in new Show install metadata | A released omission is unknown and not satisfied; a canonical omission is invalid. Serving is withheld unless the released shape's bounded probe succeeds | C2, C10 |
| Memory development-provider selection (`provider=development`, interpreter identity and compatibility projection) | `AVIBE_MEMORY_DEV_RUNTIME` + direct interpreter probe | Intentionally not persisted in managed state -> remains a Memory adapter result selected before the shared snapshot | Missing env means use the managed provider; configured but unusable means a development-provider error, never fallback through or mutate managed state | C18 |
| Memory immutable build contract (`release_state`, EverOS/Python versions, lock digest/id, uv version) | manifest validation | Only runtime version/admission outcome today -> new Memory metadata persists the validated extension or a revision that names it | A version-permitted released omission is unknown; a present malformed field is error. Either needs explicit read-only re-admission or repair before that capability is ready | C11-C12, C25 |
| Memory admission revision/result | live compatibility probe in `status()` | New pointer yes, released pointer may omit -> canonical pointer persists it at install; successful released-shape probes write only a best-effort cache under `<runtime_dir>/derived/admission/`, keyed by verified artifact identity + revision and consulted only after cheap artifact verification | Not attempted, rejected, and inspection error remain distinct and are never admitted/ready or durably cached. Cache write failure keeps a successful projection and falls back to process-wide memory | C2, C11, C21-C22 |
| Derived admission-cache entry (artifact identity, admission revision, successful result) | successful read-only re-admission | Absent today -> shared confined atomic writer under `<runtime_dir>/derived/admission/`; never installed state | Missing/stale/unreadable/unsafe is a cache miss, not failed admission. A write-path redirect or failure leaves external and installed bytes unchanged and falls back to memory | C11, C21-C22, C24 |
| Memory provider-root compatibility (`provider_root_format`, compatible formats, artifact fingerprint) | manifest-derived candidate | Current pointer yes, older shapes may omit -> canonical pointer requires all applicable fields | Version-permitted omission makes the capability unknown; malformed content is error. Neither invents a compatible format or relabels the binary absent | C11-C12, C25 |
| Memory sync contract (revision, argv, bootstrap/scrubber digests) | manifest extension | Current pointer yes when declared -> canonical pointer persists the complete declared contract | Version-permitted omission makes sync unavailable, not the artifact absent; malformed content is admission error for sync | C11-C12, C25 |
| Tmux schema and preparation/admission facts (required utf8proc/terminfo contract, runnable exact version, macOS preparation result) | manifest + install-time/live probes | Released schema 1 requirements exist but omit the leaf digest -> exact released schema 1 stays legacy; canonical schema 2 requires source and installed-byte digests plus requirements and admission revision | Unknown requirement/admission is not ready; checksum omission is accepted only for the released schema 1 fixture, never a new schema 1 default | C2, C7, C15, C23 |
| Model Hub durable claim target | selected manifest target | Schema 1/2 persist five fields including `manifest_sha256` -> new schema persists normalized platform-artifact target; platform labels normalize through the same spec alias map used for selection/admission | Dual-read and compare normalized platform-artifact identity; invalid or genuinely incompatible targets are explicit claim errors | C4, C9, C14 |
| Model Hub durable operation state (`state`, generation, error, reason) | `install-state.json` | Schema 1/2 persist it -> dual-read separately from installed snapshot | A released `not_installed` record is stale when an admitted disk artifact exists; it cannot override installed truth | C2, C13-C14 |
| Remote manifest cache (`downloads/manifest-<url-digest>.json`) | successful remote fetch | Present beside archives -> remains a typed durable cache entry | Missing under configured offline mode means selected facts were not attempted; present-but-invalid bytes are `manifest_invalid`, not a cache miss. Retention preserves either for diagnosis until explicit repair replaces it | C3, C16-C17, C19 |
| Archive ownership/protection (verified digest, released `archive_name` alias, current/rollback references, unknown names) | archive metadata + pointer traversal | Archive bytes/references exist -> canonical report classifies only recognized archive entries; derived admission state lives outside `downloads/` | Unprovable ownership/protection means retain. Unknown non-archive names and derived state are never archive candidates | C8-C10, C15-C16 |

### Normative Predicate Census

The review-loop breaker requires more than fixing the reviewed examples. This is
the result of scanning every fact row above, every scenario row below, and each
normative contract and stop point for a value, label, digest, schema, or row that
could cover two states with different evidence or permitted actions. Sharing a
row is allowed only when both the evidence and every prescribed action are the
same. “Scan” identifies distinctions found by that full pass rather than by the
five findings on `255fb3c35`.

| Predicate location | States that must not collapse | Prescribed behavior and proof | Source |
| --- | --- | --- | --- |
| Install-tree presence / C8 | Proven empty vs present-but-partial or uninspectable | Empty permits first install; partial is unknown/error, deletes nothing, and repairs only with a valid target under the guard | Already distinct |
| Manifest acquisition / C2 and C17 | Source not reached vs reached bytes invalid | Both preserve disk-local status/resolution. A transient fetch failure keeps the admitted runtime operational and reports the fetch error; invalid bytes make `ensure()`/repair fail `manifest_invalid`, never successful reuse | F3 |
| Manifest acquisition / C19 and C2 | Network deliberately not attempted vs attempted and failed | Configured offline performs zero network access and reports `not_attempted_offline`; an attempted fetch retains its `manifest_unavailable` error. Neither manufactures selected facts | Scan |
| Manifest selection / C20 and C6 | Manifest source missing vs valid manifest with no host artifact | `ensure()`/repair returns `manifest_missing` for the first and `platform_unsupported` for the second; disk-local status/resolution and retention remain available | Scan |
| Admission / C11, C21, and C22 | Validation not attempted vs attempted rejection vs attempted inspection error | Project `unknown`, `rejected`, and `error` respectively. None is ready or persistently cached as admitted; only a successful probe may populate derived cache | Scan |
| Tmux manifest version / C15 and C23 | Exact released relaxed contract vs canonical strict contract | The released schema 1 fixture alone may omit the leaf checksum; canonical schema 2 requires it. A novel or modified schema 1 omission is invalid | F1 |
| Tmux binary integrity / C23 | Pre-preparation source bytes vs post-preparation installed bytes | Verify the manifest leaf digest before preparation; after real byte-changing codesign, persist and verify a separate installed digest. One digest cannot prove both phases | F2 |
| Required fields / C10, C12, and C15 | Omission permitted by a released version vs omission from canonical state | Project the released shape's named unknown/legacy mode without rewriting it; reject a canonical omission. Schema identity, not field absence alone, selects the rule | Scan |
| Memory provider selection / C18 | Development override configured vs managed provider selected | Test the override before snapshot construction. The development branch touches no manifest or managed state; an unusable override reports its own provider error and does not fall through | F5 |
| Entrypoint resolution / C1 and Step 3 | Field omitted with a safe declarative default vs explicit/default path unsafe | Persist the safely derived path in canonical state; reject an escaping or unresolvable path. Literal presence is not the safety predicate | Scan |
| Platform admission / C7 | Literal mismatch but alias-equivalent vs genuinely incompatible | Normalize with the one spec map across selection, disk admission, resolver, and claim; reject only the incompatible control | Already distinct |
| Model Hub operation state / C13 and C14 | Stale terminal failure vs active installing claim vs admitted disk truth | Disk truth stays operational; a stale failure cannot hide it; only an active normalized same-target claim resumes | Scan |
| Remote manifest cache / C16-C17 and C19 | Cache absent vs cache present but invalid vs recognized valid cache | Absence under configured offline is `not_attempted_offline`; invalid content is `manifest_invalid`; valid content supplies selected facts. Retention preserves the typed entry | Scan |
| Downloads retention / C16 | Verified unprotected archive vs protected archive vs manifest cache vs unknown name | Delete only the first. Protection uncertainty, non-archive ownership, or unknown type always retains the entry | Already distinct |
| Preview/mutation guard / Step 2 | Real contention vs guard path unavailable/unsafe/raced | Return typed `busy` and `guard_unavailable` outcomes; neither is flattened to “already running,” and preview never creates the guard | Scan |
| Automatic cleanup / Step 4 | Protection inspection failure vs candidate deletion failure vs successful install | Both maintenance failures leave the committed install/claim successful and have distinct reports; inspection uncertainty deletes nothing | Scan |
| Derived-cache write / C24 | Installed-state mutation vs derived best-effort cache write; confined path vs redirected path | Derived write uses the shared no-follow confined writer without the mutation guard. A symlink/reparse/hard-link/race leaves the external target unchanged and degrades to memory | F4 |
| Tar extraction / Step 3 | Filter keyword unavailable vs archive rejected by either extraction path | Capability absence alone enters the guarded fallback. Filtered and forced-fallback paths accept/reject the same fixtures; an invalid archive is never reinterpreted as capability absence | Scan |

### Released-Fixture Scenario Matrix

The matrix is fixture-driven rather than function-driven. Run every applicable
row for the canonical shape and every shipped layout/schema for git, Memory,
model-hub, Show, and tmux. The dimensions are explicit: disk shape, manifest
mode (same, edited, cache-only, configured offline, fetch failed, missing,
invalid, or platform-unsupported), and host-platform relation
(same after alias normalization, selected unsupported, or genuinely incompatible
disk artifact). Each applicable row runs explicit and safe spec-derived
entrypoints. C8 runs separate clean-empty and partial-state fixtures even though
they share one fact row. “N/A” requires a product reason; adding a new persisted
fact requires a census row and at least one matrix row in the same change.

| ID | Disk fixture | Manifest / platform | `status` result | Resolver result | `ensure` result | Model Hub claim result | Retention result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C1 | Canonical admitted install | Same selected artifact / host admitted after alias normalization | Installed identity from disk; selected matches | Same admitted path, including a safe spec-derived default | Reuse, `changed=false`, no download | No stale failure | Protect current + rollback |
| C2 | Canonical admitted install | Network fetch attempted and failed / host admitted after alias normalization | Installed remains true; selected facts unavailable with fetch error | Same admitted path | Reuse disk, `changed=false`, while preserving `manifest_unavailable` in the selected/update error channel; no installed-state write | Existing admitted artifact is not hidden by failure state | Protect from disk facts |
| C3 | Canonical install + valid remote manifest cache | Configured offline / same host | Installed from disk; selected facts from cache | Same admitted path | Reuse via cache, zero network access | Target comparison uses normalized cached target | Preserve manifest cache |
| C4 | Canonical install | Only another platform entry edited / same host | Installed and selected identities match | Same path | While edited manifest is online, reuse same path/archive with no download; only then run C2 | Legacy target differing only by manifest digest resumes | No protection change |
| C5 | Canonical install | This host's selected artifact edited / same host | Installed old identity; selected differs | Old admitted path remains operational | Install selected new identity; commit pointer atomically | New canonical target; old target does not resume as same | Protect old as rollback + new current |
| C6 | Canonical install | Manifest has no artifact after host-to-artifact alias resolution / same host | Installed disk fact remains; selected is unsupported | Existing admitted path remains operational | Preserve existing install and report selected-platform failure separately | No target is manufactured | Protect existing |
| C7 | Disk artifact whose platform label is either alias-equivalent or genuinely incompatible | Any manifest mode / current host | Alias-equivalent control is admitted as C2; genuinely incompatible artifact is present but rejected | Admitted path for alias-equivalent control; `None` with mismatch evidence otherwise | Reuse alias-equivalent artifact; install only a valid host target for a genuine mismatch | Target uses normalized artifact label; genuinely incompatible target does not match | Foreign artifact retained until safely classified |
| C8 | **Empty:** successfully inspected tree with no pointer, metadata, versions, or install evidence. **Partial:** any present malformed, unreadable, escaping, or incomplete pointer/metadata state | Available or unavailable / any host | Empty is `absent`; partial is `unknown` or `error`, never absent | `None` for both | Empty may perform a first install under the mutation guard; partial repairs only under that guard with a valid selected target | Empty may start a claim after target resolution; partial starts none before repair/target resolution | Empty protects nothing; partial deletes nothing whose protection is uninspectable |
| C9 | Released shared whole-manifest-digest directory | Other platform edited, then cache-only/unavailable / same host | Adopt installed platform identity without write | Released path | Online edited manifest reuses without download; offline reuses disk | Schema 1/2 target normalizes without `install_target_changed` | Protect released layout/archive |
| C10 | Released Show metadata/layout without `minimum_node` | Unavailable / same host | Installed; Node compatibility unknown, not satisfied | Artifact entrypoint exists; serving command gated by bounded probe | No inspection write/download; explicit install may persist a known requirement | N/A | Protect released layout/archive |
| C11 | Released Memory pointer without admission revision | Probe supported and succeeds / same host | Admitted projection; best-effort cache by verified identity | Path; fresh managers/processes reuse a durable success cache | Reuse; explicit mutation may persist canonical fields, but inspection never changes the released pointer | N/A | Protect regardless of cache-write failure; pointer and artifact bytes remain identical |
| C12 | Released Memory pointer with version-permitted omitted immutable-build, provider-root, or sync fields | Any manifest mode / same host | Artifact and each omitted capability reported separately; no invented contract or compatibility | Binary only if core admission passes; omitted capability withheld | Explicit mutation may canonicalize from a valid manifest | N/A | Protect released artifact |
| C13 | Admitted Model Hub install + released `state=not_installed` | Any manifest mode / same host | Installed truth wins; persisted failure is projected stale | Engine path | Reuse install; no unnecessary claim/download | Stale failure neither hides runtime nor becomes active claim; inspection does not rewrite it | Protect engine/archive |
| C14 | Released Model Hub `installing` claim schema 1/2 | Manifest differs only outside host artifact / host admitted through literal or aliased label | Installing/admitted facts remain distinct | Disk path if already admitted | Resume only normalized same target | Resume without target-changed; malformed or genuinely incompatible target fails explicitly | Protect claimed/current bytes |
| C15 | Released tmux schema 1 manifest/metadata/pointer without `binary_sha256` | Cache-only/unavailable / same host | Installed with explicit legacy integrity level | Path only after runnable/exact-version admission | Reuse without download; inspection never upgrades metadata | N/A | Protect released tmux archive/install |
| C16 | Manifest cache + verified/stale archives + unknown download names | Configured offline / same host | Cache remains usable for selected facts | Existing admitted path | Offline reuse/repair can read cache | Claim behavior unchanged | Delete only typed, unprotected archive candidates; preserve `manifest-*` and every unknown non-archive entry |
| C17 | Canonical admitted install + manifest bytes that parse or validate incorrectly | Reached invalid manifest / same host | Installed remains true; selected facts carry `manifest_invalid` | Same admitted path | Fail `manifest_invalid`; no successful reuse, repair, download, or installed-state write | Existing admitted artifact remains visible; no target is manufactured | Protect from disk facts |
| C18 | `AVIBE_MEMORY_DEV_RUNTIME` points to a usable development interpreter; managed tree contains a poison fixture | No manifest mode applies / compatible host | `provider=development`, ready | Development interpreter path | Success, `changed=false`, including `force=true` | N/A | Zero manifest, metadata, pointer, claim, derived-cache, or retention access |
| C19 | Canonical admitted install + no manifest cache | Configured offline / same host | Installed remains true; selected facts are `not_attempted_offline` | Same admitted path | Reuse disk, `changed=false`, zero network and installed-state writes | Existing admitted artifact remains visible; target comparison is not attempted | Protect from disk facts |
| C20 | Canonical admitted install + absent configured/packaged manifest source | Source missing / same host | Installed remains true; selected facts carry `manifest_missing` | Same admitted path | Fail `manifest_missing`; no target or successful repair is invented | Existing admitted artifact remains visible; no claim starts | Protect from disk facts |
| C21 | Released Memory pointer without admission revision | Probe supported and returns rejection / same host | Installed artifact present; admission `rejected`, not ready | `None` with rejection evidence | Explicit mutation may repair from a valid manifest; inspection does not write/cache | N/A | Protect released artifact; derived success cache absent |
| C22 | Released Memory pointer without admission revision | Admission projection cannot be attempted / same host | Installed artifact present; admission `unknown`, not ready | `None` with not-attempted evidence | Explicit mutation may repair from a valid manifest; inspection does not write/cache | N/A | Protect released artifact; derived success cache absent |
| C23 | Canonical tmux schema 2 manifest | Source leaf digest valid; macOS signing fixture really changes bytes / same host | Installed only after post-preparation digest and runnable/version admission | Prepared tmux path | Verify source digest, mutate bytes, persist distinct installed digest, admit; source mismatch runs no preparation | N/A | Protect schema 2 archive/install and both integrity facts |
| C24 | Released Memory artifact that passes re-admission + derived-cache parent/target symlink, hard-link, reparse, or identity-race fixture | Any manifest mode / same host | Successful projection survives cache-write rejection via process memory | Same admitted path | Reuse without installed-state mutation | N/A | External redirect target, pointer, metadata, and artifact tree remain byte-for-byte unchanged |
| C25 | Released Memory pointer with a present malformed immutable-build, provider-root, or sync field | Any manifest mode / same host | Core artifact stays separately inspectable; malformed capability is `error`, not version-permitted omission | Binary only if independent core admission passes; malformed capability withheld | Explicit repair requires a valid manifest; no inspection canonicalization | N/A | Protect released artifact and preserve malformed evidence |
| C26 | Released Memory pointer without admission revision | Probe starts and raises/times out / same host | Installed artifact present; admission `error`, not ready | `None` with inspection error | Explicit mutation may repair from a valid manifest; inspection does not write/cache | N/A | Protect released artifact; derived success cache absent |

## Migration Sequence And Stop Points

Every step is independently shippable. Do not begin the next step until the
acceptance property for the current step holds.

### Step 1: Make The Shared Disk-Fact Snapshot Truthful

Change the shared platform-artifact identity, add dual-read adoption for its old
manifest-digest directories, and introduce the local installed-artifact
snapshot. Treat Model Hub's released durable claim target as a separate
versioned shape: dual-read schema versions 1 and 2, normalize their five fields
to the four platform-artifact identity fields for comparison, and write a new
claim shape only under a bumped schema version. Preserve Memory's development
provider selection before any snapshot or manifest/state access, then add its
non-mutating admission projection before routing released active pointers
through the snapshot. Use one declarative host-to-artifact alias mapping for
manifest selection, disk admission, resolver output, and claim comparison;
Model Hub's released `linux-amd64` artifact is valid on a `linux-x64` host.
Persist a successful deterministic re-admission only as an atomic, best-effort
derived cache under `<runtime_dir>/derived/admission/`, keyed by the verified
artifact identity and admission revision. Use the shared confined no-follow
writer: cache creation does not acquire the mutation guard or alter a released
pointer, and an unsafe or unwritable destination degrades to process memory.
Keep current public payloads as projections during this step.

**Stop point:** git, Memory, and model-hub use the new snapshot and identity;
Show and tmux are untouched. Model-hub recovery can resume every released claim
shape without treating a manifest-only digest change as a target change.

**Acceptance test:** run C1-C9, C11-C14, C17-C22, and C24-C26 for every
applicable git, Memory, and model-hub released fixture. C4 is ordered: while the
edited manifest is online, `ensure()` reuses the original path/archive with no
download; only afterward make the fetch fail and run C2. Under C2, every
observable column is mandatory: shared `status()` reports disk identity, Git's
resolver returns the admitted path, model-hub's resolver returns the admitted
engine, and `ensure()` reuses without an installed-state write while preserving
the fetch error separately. C17 uses that disk fixture with reached invalid
manifest bytes: status and both resolvers remain operational, but `ensure()` and
repair return `manifest_invalid` and may not report successful reuse. A
status-only or resolver-only test does not pass. C19 proves configured offline
performs zero network access; C20 proves a missing source is not reported as
platform unsupported.

C7 includes a released Model Hub `linux-amd64` pointer, metadata, and claim on
`linux-x64`; status, resolver, ensure, and claim comparison admit the
alias-equivalent identity, while a genuinely incompatible label fails. C8
proves both clean first install and fail-closed partial inspection. C11, C21,
C22, and C26 separately run successful, rejecting, not-attempted, and
raising/timed-out Memory admission projections and byte-compare the released
pointer and artifact tree. After C11 succeeds, fresh managers and simulated
fresh CLI processes reuse the identity-keyed durable result without rerunning
the subprocess, but only after cheap pointer/metadata/artifact verification.
Changing the pointer identity makes the old entry ineligible immediately. A
read-only cache directory leaves the successful projection intact and uses the
process-wide result; the other three outcomes remain uncached.

C18 poisons every manifest and managed-state reader/writer, then proves
`provider=development`, both resolvers, and forced `ensure(changed=false)`
complete with zero calls. C24 runs successful re-admission against parent and
target symlink/hard-link fixtures plus Windows reparse and identity-race
simulations: every external target, pointer, metadata file, and artifact stays
byte-for-byte unchanged and the result falls back to process memory. C13 seeds
an admitted engine with every released `not_installed` state and proves the
failure is stale. Every census row used by these dependencies must name a
passing scenario; a blank cell blocks the step.

**What breaks:** expected functional breakage is **none**. Without dual-read
adoption, all three can appear missing or redownload while offline; that failure
blocks the step. Without claim normalization, an in-flight Model Hub operation
can fail as `install_target_changed`; that also blocks the step. Downstream
status consumers that currently call the selected version “installed” must be
updated in the same step. Treating a Memory admission failure as absence or
mutating its released pointer during inspection also blocks the step. Creating
a new directory/download while C4's selected host artifact is unchanged, or
letting C13's stale failure override an admitted engine, proves the source of
truth did not move completely and blocks the step. Re-probing an unchanged
released Memory identity in a fresh process, failing status because the derived
cache is unwritable, rejecting Model Hub's released `linux-amd64` identity on
`linux-x64`, or classifying C8's proven-empty tree as unknown would also block
the step. Reading managed state after selecting the Memory development provider,
following a derived-cache redirect, flattening rejected/not-attempted/errored
admission, or silently reusing disk after `manifest_invalid` also blocks it.

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
`.install.lock`. The derived-admission cache is not installed state: its
best-effort confined atomic write does not use this mutation guard, and racing
writers of the same identity/revision must converge to the same valid payload
without following a symlink, hard link, or reparse point. C24's redirected
external target remains unchanged and write failure falls back to memory. A
guarded identity change makes all old cache keys ineligible at pointer commit; physical
pruning of those now-stale derived files remains best-effort maintenance.

**What breaks:** no installed runtime. A new
`<runtime>_install_guard_unavailable` reason is an intentional API correction;
any consumer that only recognizes `<runtime>_install_already_running` must be
updated before this stop point. Loosening `MigrationFileLock` globally is out of
scope; the managed-runtime guard must open and validate its own descriptor.

### Step 3: Add Composite Artifacts Without Weakening Binary Artifacts

Add the declarative metadata/provider filename, directory-artifact verifier,
manifest-extension validator and persistence map, and internal-link extraction
policy. The extractor change is atomic: allow only `REGTYPE`, `AREGTYPE`,
`DIRTYPE`, `SYMTYPE`, and `LNKTYPE`; validate both `name` and `linkname` against
the extraction root; and extract one member at a time in archive order. A link
must resolve to content already extracted. Do not use a two-pass or parallel
extractor. Defaults remain exactly the current strict binary contract.

**Stop point:** a fixture directory artifact can install through
`ManagedRuntimeManager`; Show is not yet cut over. This step cannot stop between
removing link rejection and adding link-target confinement.

**Acceptance test:** run the same hermetic probe used to establish this contract:
one regular executable, one symlink, and one hard link. The shared extractor's
benign result is exactly `extracted ['hard.bin', 'link.so', 'real.bin']`; both
links remain inside the root, and the hard-link target exists and is executable.
For a malicious `linkname="../../../../etc/passwd"`, it raises `ValueError` with
`link target` in the message. Benign success and malicious rejection must land
in the same commit. Run both fixtures once with `filter="data"` available and
once with `extractall` stubbed to raise `TypeError` for the filter keyword,
forcing the manually guarded fallback without an old-interpreter matrix. The
two paths must accept the same benign archive and reject the same malicious
archive; a path-dependent result blocks the step. Existing git, Memory, and
model-hub specs still reject a missing `binary_sha256`, an absent or unsafe
**resolved** binary path, or any link member. Any archive entry that omits
`bin_path` but has a safe `ManagedRuntimeSpec.default_bin_path` remains
accepted, and canonical metadata/pointer output persists that resolved default;
an unsafe explicit or default path is rejected. The directory fixture also
persists its declared runtime prerequisite atomically. Keep the declared Python
floor at `>=3.10`: Python 3.10/3.11 patch releases without extraction filters
take the tested fallback, while backported releases use the filtered path by
capability.

**What breaks:** expected breakage is **none** before Show cuts over. If optional
leaf checksums or link acceptance become the default, integrity for git,
Memory, and model-hub breaks. If link acceptance is omitted, Show installation
fails at the first symlink on every Darwin and Linux archive; if hard links or
archive order are mishandled, the esbuild binary is unavailable and Show page
builds fail for every non-Windows user. If link rejection is relaxed without
`linkname` hardening, the migration creates the sole extractor without link
escape protection. Requiring literal `bin_path` presence would break accepted
manifests that rely on the shared spec default; weakening safe-path validation
would make that compatibility path unsafe. Retaining the current version gate
would also make Python 3.10.12+ and 3.11.4+ skip a security filter they provide.
Every case blocks Step 5.

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
retains the inspection error. Run C16 with a real
`downloads/manifest-<url-digest>.json` and representative unknown non-archive
names: cleanup preserves all of them, and the manifest cache remains usable for
offline status, repair, and archive probing. Seed derived-admission entries too:
they are outside the archive namespace, cannot enter the archive candidate set,
and an unknown or stale entry cannot weaken protection. Re-run and assert
idempotence.

**What breaks:** no runnable dependency. Stale unprotected archives and installs
are deleted by design. If any current or retained install lacks an admitted
archive identity, cleanup must skip rather than delete; a deletion in that case
breaks git, Memory, or model-hub rollback safety and blocks rollout for that
spec. Propagating an automatic-cleanup error through `ensure()` breaks an
already-committed install and Model Hub claim state, and also blocks rollout.
Deleting a remote manifest cache or any unknown non-archive name breaks offline
repair/probing and proves the downloads namespace was classified by location
rather than ownership; that blocks every spec using `manifest_url`.

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
manifest, configured-offline reuse, fetch failure, reached-invalid and missing
manifest, failed and forced replacement, unrelated-platform
manifest edits, previous fingerprints, legacy parent layout, configured source
lineage, internal symlinks, Node missing/unsupported, archive cleanup, Doctor,
and dependencies status. The link case includes the esbuild hard link and
asserts its target remains executable. Add an invariant test that an injected
status inspection failure remains unknown/error and is never projected as absent. With
the manifest unavailable, a new metadata record plus downgraded Node must remain
unsupported, while every released record lacking `minimum_node` must remain an
installed artifact with unknown compatibility and cannot become serving-ready
without a successful bounded readiness probe.

**What breaks:** git, Memory, and model-hub have **no new behavior** in this
step. Show would break for already-installed/offline users if any released
metadata/provider/layout is omitted, and would break for normal archives if
safe internal symlinks and hard links are not enabled only for its spec.

### Step 6: Delete Show's Parallel Installer

Delete unreachable Show manifest installer, cache, lock, cleanup, and exact
utility functions. Reduce retained same-name methods to product wrappers; they
must not contain a second manifest transaction or filesystem policy.

**Stop point:** direct archive and npm are visibly separate Show providers; the
Show manifest provider has exactly one implementation. Tmux remains temporarily
on its measured legacy installer until Step 7.

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

### Step 7: Migrate Tmux And Enforce Sole Ownership

Express tmux as a `ManagedRuntimeSpec` consumer after Show has validated every
shared capability. Retain only tmux's public result projection, macOS
quarantine/codesign preparation, runnable/exact-version admission, and
utf8proc/terminfo manifest requirements. Dual-read the released schema 1
manifest, `.avibe-tmux-runtime.json`, `current.json`, and legacy two-level
install directory. Canonical manifests are schema 2 and require
`binary_sha256`; only the exact released schema 1 fixture may omit it. Legacy
installs remain explicitly archive-verified legacy admission and are never
upgraded by a status write. Canonical metadata records the verified source leaf
digest and a separate post-preparation installed digest because macOS codesign
may change the binary.

**Stop point:** final architecture. All five dependencies use
`ManagedRuntimeManager`; Show and tmux contain product adapters, not filesystem
or install engines.

**Acceptance test:** seed the exact packaged schema 1 manifest and every
released tmux metadata/pointer/layout fixture and resolve it offline without a
download or inspection write. A modified or novel schema 1 fixture missing
`binary_sha256` is `manifest_invalid`. Run C23 with a schema 2 manifest and a
real signing fixture whose command changes the binary bytes, not a stubbed
“changed” flag: verify the source leaf digest before preparation, prove the
source-mismatch control never invokes preparation, run quarantine removal and
codesign, persist a different installed digest, and use that digest plus exact
version/utf8proc/terminfo admission for immediate and offline resolution. Cover
codesign command and verification failure separately. Re-run the
benign/malicious link probe through the shared extractor. An AST ownership test
rejects Show or tmux
definitions/call paths for shared manifest loading, archive verification/cache,
identity, metadata/current writes, mutation lock, retention, platform tagging,
hashing, or extraction. Run focused suites for all five managers and their
CLI/Doctor/dependencies consumers.

**What breaks:** git, Memory, model-hub, and Show have **no new behavior**. Tmux
breaks if the released manifest's missing `binary_sha256` is treated as corrupt,
if its current pointer is rewritten during status, or if preparation/admission
hooks run in a different transaction phase. Treating a new schema 1 omission as
legacy, checking manifest bytes only after codesign, or comparing the prepared
binary to the pre-preparation digest also blocks deletion of the tmux installer.

## Measured Size Estimate

Measurements use AST function spans (`end_lineno - lineno + 1`) at
`903414cc4`; blank lines outside functions, imports, dataclass declarations,
serving-only functions, and tests are excluded. The install spans were
revalidated at `2c6fcbe88`; the only Show diff is serving-side. The two current
outliers total **4,069 physical lines**: 3,370 in the supplied Show audit plus
699 in tmux.

The arithmetic below is a historical planning snapshot measured at those
revisions. It is not an acceptance criterion and is not maintained as later
contract discoveries add fields, fixtures, or small seams. Those discoveries
do not change the measured source spans or ownership decision; implementation
records the actual diff instead of repeatedly forecasting code not yet written.

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
  code should require **510-610 net new production lines in
  `core/managed_runtime.py`** before tmux. The budget is 260-310 for archive
  retention, 40-70 for guard hardening, 90-120 for disk/legacy admission,
  90-110 for composite-artifact/spec/result plumbing, and 30-50 for the
  identity-keyed derived-admission cache. That last range is anchored by the
  existing 32-line Model Hub verification-cache key and the existing shared
  atomic JSON writer; it adds persistent schema/read/fallback handling but
  reuses the snapshot's identity verification.

The resolved extraction-filter decision does not change this estimate: it
replaces the two version checks with Show's existing capability-detection
pattern inside the already-budgeted extraction work.

Tmux has **38 functions / 568 function-span lines**. Its measured decomposition
is 23 common installer functions / 451 lines, eight product helpers / 92 lines,
three released-layout helpers / 17 lines, four public wrappers / 8 lines, and
131 non-function lines. The existing shared hooks already express staged binary
preparation, binary admission, and version reporting. A 160-210-line tmux spec,
product hook, compatibility data, and wrapper target therefore deletes
**489-539 lines**; only **20-50 additional shared lines** should be needed after
the Show seams exist.

| Historical estimate (non-acceptance) | Show / Steps 1-6 | Tmux / Step 7 | Five-dependency total |
| --- | ---: | ---: | ---: |
| Lines deleted from outlier modules | 1,560-1,650 | 489-539 | **2,049-2,189** |
| Existing behavior moved to the shared owner | 732 | 451 | **1,183** |
| Lines added to the shared layer | 510-610 | 20-50 | **530-660** |
| Net production reduction | 950-1,140 | 439-519 | **1,389-1,659** |

“Moved” is semantic ownership and overlaps deletion; it is not a promise to
copy text. The net row is deletion minus shared growth. It excludes the already
assigned 212-line GitHub-provider removal and does not count test changes.

The mechanical size gate replaces range maintenance: at the deletion stop
point, every symbol scheduled for deletion has zero definitions and call-site
hits in source and tests, and the implementation records actual lines deleted,
moved by ownership, and added to the shared layer. A forecast falling outside a
range above is not by itself a defect; a surviving duplicate owner is.

The exact endpoint cannot be known without choosing the staged-entrypoint result
shape. The experiment that narrows the Show range is a compile-only Step 3 fixture:
implement the smallest directory-artifact subclass without touching Show, then
remeasure which Show wrappers can become direct projections. The experiment for
the tmux range is a compile-only spec using the released schema 1 fixtures and
the three existing binary hooks. Do not use an upper estimate as permission to
introduce a general callback framework.

### Concept Count

The count is derived from behavioral owners, not filenames. Seven common
capabilities are:
primitives/extraction, manifest selection, archive acquisition/cache, install
transaction/identity/persistence, mutation/preview locking,
retention/cleanup, and status/probe. Shared and Show each own all seven; tmux
owns six, omitting retention/cleanup. Four Show product concepts remain:
admission/availability, Node prerequisite/command, direct archive, and npm. Two
tmux product concepts remain: macOS platform preparation, and
runnable/version/utf8proc/terminfo admission. The predicate census also makes
Memory's explicit development-provider override a factual product concept: it
runs before the managed snapshot and cannot be absorbed into manifest selection
without changing its zero-managed-state contract.

The following arrow is the original forecast measured with the line snapshot
above. It predates discovery of the Memory development-provider concept and is
retained for provenance only; it is neither a current exhaustive count nor an
acceptance target, and later contract findings do not cause it to be recomputed.

| Historical state | Common owner instances | Show product concepts | Tmux product concepts | Total |
| --- | ---: | ---: | ---: | ---: |
| Before | shared 7 + Show 7 + tmux 6 = 20 | 4 | 2 | **26** |
| After | shared 7 | 4 | 2 | **13** |

The acceptance rule is instead mechanical: enumerate common and product owners
at each deletion stop, preserve every listed product concept including Memory's
development provider, and require the total concept count to decrease strictly.
All Show/tmux common owner instances scheduled for deletion must also have zero
source and test hits. Grouping the two tmux product clusters differently is
allowed only if it changes before and after inventories equally; platform
signing and runtime compatibility remain separate here because they fail for
independent product reasons.

The target adds seams, data fields, and derived cache entries, not another
engine concept: admission status/probe remains their single behavioral owner. A
“generic archive-cache helper” owned outside `ManagedRuntimeManager` would add
an owner and is therefore rejected unless it has an independent non-runtime
consumer.

## Resolved Archive-Link Contract

The previous hard-link unknown is resolved by enumerating every Show v3.0.13
platform archive:

| Platform | Members | Regular | Directories | Symlinks | Hard links |
| --- | ---: | ---: | ---: | ---: | ---: |
| darwin-arm64 / darwin-x64 | 8,376 | 7,869 | 490 | **16** | **1** |
| linux-arm64 / linux-x64 | 8,376 | 7,869 | 490 | **16** | **1** |
| win32-arm64 | 8,530 | 8,034 | 496 | 0 | 0 |
| win32-x64 | 8,534 | 8,037 | 497 | 0 | 0 |

The hard link joins `node_modules/esbuild/bin/esbuild` and
`node_modules/@esbuild/<platform>/bin/esbuild`. Which member is the link and
which is the real file reverses between Darwin and Linux because of archive
member order. Sequential per-member extraction succeeds for both; a two-pass or
parallel “optimization” fails one of them.

The hermetic three-extractor probe established the current behavior:

```text
managed_runtime benign -> ValueError: Unsupported managed runtime archive member: link.so
show_runtime benign    -> extracted ['hard.bin', 'link.so', 'real.bin']
tmux_runtime benign    -> extracted ['hard.bin', 'link.so', 'real.bin']

managed_runtime malicious -> ValueError: Unsupported managed runtime archive member: link.so
show_runtime malicious    -> ValueError: Unsafe archive link target: hard.bin
tmux_runtime malicious    -> ValueError: Unsafe tmux archive link target: hard.bin
```

All three reject the malicious fixture today, but shared rejects for the wrong
reason: it never reaches the malicious `linkname`. Shared is currently safe
because of a restriction Step 3 must remove. Removing it without simultaneously
adding `linkname` confinement would make the sole-owner extractor the only one
without link-escape protection. After Step 3, the same probe must produce:

```text
managed_runtime benign    -> extracted ['hard.bin', 'link.so', 'real.bin']
managed_runtime malicious -> ValueError: <message containing "link target">
```

### Named Unknown: Extraction-Filter Capability (Resolved)

**Original question:** should the migration retain and test a Python 3.10/3.11
fallback, or raise the project minimum to Python 3.12?

**Resolution:** neither branch of that question describes the actual capability
boundary. Three facts determine the contract:

1. The project declares `requires-python = ">=3.10"`.
2. [PEP 706](https://peps.python.org/pep-0706/) extraction filters were
   security-backported in
   [Python 3.10.12](https://docs.python.org/3.10/whatsnew/3.10.html#tarfile)
   and
   [Python 3.11.4](https://docs.python.org/3.11/whatsnew/3.11.html#tarfile).
   Python's own
   [compatibility guidance](https://docs.python.org/3.12/library/tarfile.html#supporting-older-python-versions)
   therefore requires capability detection rather than a Python-version test.
3. Show already uses the required `try: extractall(filter="data")` /
   `except TypeError` capability pattern. Shared and tmux instead gate the
   filter on `sys.version_info >= (3, 12)`, so they currently skip protection
   that is available on supported 3.10.12+ and 3.11.4+ interpreters.

Do not raise the Python floor: that would discard the default interpreters on
Ubuntu 22.04 and Debian 12 to remove a fallback that the product can test
hermetically. Step 3 moves Show's capability pattern into the sole shared
extractor and preserves a manually guarded fallback for patch releases that do
not support the keyword; no path may call unfiltered extraction without those
guards. Link acceptance, `name`/`linkname` confinement, and filter/fallback
equivalence are one atomic change. The fallback test stubs the filter call to
raise `TypeError` and proves that the same malicious fixture has the same
rejected outcome; no old-interpreter matrix is required. Step 7 deletes tmux's
version-gated branch when tmux adopts the shared extractor.

There is no residual product or release decision for archive extraction.

## Non-Goals

- No change to Show serving or request behavior.
- No resurrection or compatibility path for the removed GitHub provider.
- No attempt to force direct archive or npm into the manifest security model.
- No deletion of released on-disk compatibility readers.
- No issue filing as part of this design PR.
- No dependency addition.
