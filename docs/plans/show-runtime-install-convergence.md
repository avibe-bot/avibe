# Show Runtime Install Convergence Contract

Status: bounded migration contract

Scope: analysis and design only; no production implementation

Code baseline: `2c6fcbe88` (`origin/master` when the install audit was
revalidated on 2026-08-23). The original function-span measurement was made at
`903414cc4`. The GitHub source provider is assumed gone before migration.

## Decision And Boundary

`core/managed_runtime.py` is the target owner of declarative manifest
installation for git, Memory, model-hub, Show's manifest provider, and tmux.
Show and tmux remain product adapters. Show keeps policy/install/serving
projection, the system Node prerequisite and command. Memory keeps its explicit
development provider and its controller-coordinated activation boundary. Tmux
keeps macOS preparation and its runtime compatibility projection. At the
measured baseline, direct-archive persists no install metadata and npm acquires
no archive, so neither can supply the admitted-disk-record model. Their final
ownership awaits the W3 source-ladder decision; this contract does not assume
either delegates to the shared layer. Show serving
(request proxying, lifecycle, readiness, prewarm, WebSocket routing, context
capability, and stop) is outside this migration.

The validation boundary is intentionally finite. This document specifies the
in-flight Step 1, records current behavior measured at named revisions, and
defines only an intent and a gate for Steps 2-7. Their detailed contracts are
written after the preceding step lands and its resulting code can be measured.
Forecasts are planning history, not acceptance criteria.

## Per-Function Verdict And Drift Ledger

This table is a non-normative audit census measured at `2c6fcbe88`. **Subset**
means delete Show's copy and call shared code. **Superset** means adopt shared
code and extend the named general seam. **Different** means a product
requirement keeps a Show adapter while common mechanics move underneath it.
Every one-sided behavior is classified as a deliberate Show requirement or a
latent defect in the side that lacks it.

| Pair | Verdict | One-sided behavior and classification | Measured at |
| --- | --- | --- | --- |
| `status` | **Different:** shared installed snapshot plus Show projection. | **Show requirement:** policy/install/serving dimensions, Node, and provider dispatch. **Shared defect:** selected-manifest failure hides a persisted install and can pair selected version with disk state. **Show defect:** inspection failure can be projected as absence. | `2c6fcbe88` |
| `probe_archive_reachability` | **Superset:** shared manifest probe plus Show provider/reason adapter. | **Show requirement:** direct archive, npm, and Show reason vocabulary. Shared already handles `file:` URLs. | `2c6fcbe88` |
| `_clean_locked` | **Superset:** shared retention planner with legacy-layout and staging-pattern data. | **Show requirement:** released layouts and source lineages. **Shared defect:** no archive reclamation or post-install retention. **Show defect:** glob-based discovery can suppress inspection errors. | `2c6fcbe88` |
| `_preview_busy_reason` | **Superset:** shared preview with typed contention/inspection result and staging patterns. | **Shared defect:** contention and an uninspectable/replaced guard share one reason. **Show defect:** absent-lock planning does not retain its process lock. | `2c6fcbe88` |
| `_windows_preview_busy_reason` | **Superset:** shared Windows path-identity/reparse check. | **Shared defect:** weaker Windows identity check and collapsed failure reason. **Show requirement:** only staging names differ. | `2c6fcbe88` |
| `_resolve_manifest_archive` | **Superset:** shared content-addressed cache plus offline/provenance result data. | **Shared defect:** archive-name caching grows without bound. **Show requirement:** provenance drives recovery reporting. **Show defect:** the cache fast path accepts any existing filesystem entry. | `2c6fcbe88` |
| `_safe_extract_tar` / `safe_extract_tar` | **Superset:** shared composite-artifact extraction policy, defaulting to binary-strict. | **Show requirement:** released Unix bundles contain internal links. **Migration blocker:** shared safely rejects all links today; removing that restriction without link-target confinement would create a defect. **Shared defect:** its version gate skips an available backported data filter on supported Python patch releases. | `2c6fcbe88`; archives `v3.0.13` |
| `_runtime_platform_tag` / `runtime_platform_tag` | **Subset.** | No behavioral difference. | `2c6fcbe88` |
| `clean` | **Superset:** shared cleanup plus Show report adapter. | **Shared defect:** downloaded archives are omitted. **Show requirement:** CLI/Doctor payload. **Show defect:** part of real cleanup occurs before the install guard. | `2c6fcbe88` |
| `_write_manifest_install_metadata` | **Different:** shared atomic persistence plus a Show legacy-shape adapter. | **Show requirement:** released directory-artifact metadata lacks binary fields and uses a Show filename/provider. **Show defect:** direct non-atomic write and no persisted Node prerequisite. | `2c6fcbe88` |
| `_preview_lock_probe` | **Superset:** typed failure and reparse predicate in shared code. | **Shared defect:** special/uninspectable paths are mislabeled as contention. | `2c6fcbe88` |
| `_preview_guard` | **Subset.** | **Show defect:** absent-descriptor planning loses process exclusion; shared retains it. | `2c6fcbe88` |
| `_guard_path_matches_fd` | **Superset:** shared cross-platform exclusive-regular-file predicate. | **Shared defect:** Windows reparse attributes are absent from its identity check. | `2c6fcbe88` |
| `_release_preview_guard` | **Subset.** | **Show defect:** the absent-descriptor branch has no retained process lock to release. | `2c6fcbe88` |
| `_manifest_status_payload` | **Superset:** shared installed facts plus Show Node/platform projection. | **Show requirement:** Node compatibility and complete Show platform diagnostics. **Show defect:** released metadata cannot provide offline `minimum_node`. | `2c6fcbe88` |
| `_manifest_install_dir` | **Superset:** shared platform-artifact identity plus legacy candidates. | **Shared defect:** whole-manifest digest changes reinstall unchanged host bytes. Show already uses platform-artifact identity. | `2c6fcbe88` |
| `_archive_status_payload` | **Subset:** shared projection omits binary-only fields for directory artifacts. | No missing Show behavior; shared binary fields apply only to strict binaries. | `2c6fcbe88` |
| `_preview_raced_busy` | **Superset:** shared race check with declarative staging patterns. | **Shared defect:** a new staging path is ignored when the lock path is absent or replaced. | `2c6fcbe88` |
| `_downloaded_archive_matches` | **Subset.** | No semantic difference beyond helper and reason names. | `2c6fcbe88` |
| `_preview_lock_missing` | **Subset.** | No semantic difference beyond the guard-path attribute name. | `2c6fcbe88` |
| `_manifest_archive_for_platform` | **Subset.** | Shared also supports aliases; Show's released manifest uses host tags, so this is neither a Show requirement nor a defect. | `2c6fcbe88` |
| `_file_sha256` / `file_sha256` | **Subset.** | No behavioral difference. | `2c6fcbe88` |
| `_env_flag_enabled` / `env_flag_enabled` | **Subset:** use the shared truthy set. | **Show defect:** arbitrary non-false text enables policy; repository callers document `1`/`true`. | `2c6fcbe88` |
| `_safe_path_part` / `safe_path_part` | **Subset:** shared sanitizer for new paths, metadata discovery for legacy paths. | **Show defect:** `.`/`..` survive and invalid characters use a weaker mapping. | `2c6fcbe88` |

### Shared-Layer Defects Exposed By Drift

These are the separately enumerated low-cost wins in the existing shared
consumers. They are current-behavior findings, not issue filings. The rows are
non-normative and measured at `2c6fcbe88`.

| Defect in shared | Affected existing consumers | Measured consequence | Measured at |
| --- | --- | --- | --- |
| Manifest-gated installed state | git, Memory, model-hub | `status()` and `resolve_binary()` can forget an admitted disk install when no installable manifest is available. | `2c6fcbe88` |
| Selected identity reported as installed identity | git, Memory, model-hub | Status can publish the manifest version beside disk-derived installed state. | `2c6fcbe88` |
| Whole-manifest target identity | git, Memory, model-hub; claim consequence in model-hub | An unrelated platform edit changes the install directory; released model-hub claims can report `install_target_changed`. | `2c6fcbe88` |
| Mutation lock follows/under-validates its path | git, Memory, model-hub | Shared locking lacks Show's no-follow, link-count, and post-open path-identity checks. | `2c6fcbe88` |
| Preview collapses failure causes | git, Memory, model-hub | Special, replaced, or unreadable guard paths look like live contention; Windows reparse identity is weaker. | `2c6fcbe88` |
| No downloaded-archive retention | git, Memory, model-hub | Versioned downloads accumulate indefinitely. | `2c6fcbe88` |
| No post-success version retention | git, Memory, model-hub | `ensure()` never invokes existing version cleanup; Memory and model-hub have no equivalent user cleanup path. | `2c6fcbe88` |
| Version-gated tar data filter | git, Memory, model-hub; also tmux | Shared and tmux skip the backported filter on Python 3.10.12+ and 3.11.4+; their existing whitelist/path checks still apply. | `2c6fcbe88` |

Shared link rejection is not a present vulnerability: it is a Step 3 capability
gap. Four of Show's six archives cannot use the shared extractor until link
support and confinement arrive together.

## Uncounterparted Install Code

This non-normative census preserves the supplied **1,621-line** Show grouping.
Rows were measured at `903414cc4` and revalidated for install-side changes at
`2c6fcbe88`. Classification follows behavior, so mixed groups have more than
one destination without double-counting their supplied line total.

| Supplied group | Lines | Classification and destination | Measured at |
| --- | ---: | --- | --- |
| Manifest handling | 421 | **Shared capability grows:** source/cache handling, platform-artifact identity, installed-state reading, persistence, and legacy adoption benefit all five. Show manifest duplicates become **dead** after cutover. Show retains only product projection. | `903414cc4`; revalidated `2c6fcbe88` |
| Archive cache / cleanup / protection | 355 | **Shared capability grows:** content-addressed downloads, protected-set discovery, cleanup reporting, and post-install retention benefit all five. The expectation is confirmed; Show CLI/Doctor wording remains product-specific. | `903414cc4`; revalidated `2c6fcbe88` |
| Install lock and concurrency guard | 221 | **Shared capability grows:** mutation/preview exclusion and safe path identity benefit all five. Duplicate Show guard code and the duplicate exception branch become **dead**. | `903414cc4`; revalidated `2c6fcbe88` |
| Install orchestration | 195 | **Mixed:** shared manifest transaction benefits Show/tmux; Show-specific policy, providers, availability, and operation outcomes remain. Duplicate manifest orchestration becomes **dead**. | `903414cc4`; revalidated `2c6fcbe88` |
| Archive handling | 120 | **Mixed:** shared extraction/materialization grows for Show/tmux; the unpinned direct-archive source remains Show-specific because it is an operator escape hatch outside manifest integrity. Duplicate primitives become **dead**. | `903414cc4`; revalidated `2c6fcbe88` |
| GitHub provider | 104 | **Dead:** removed by the prerequisite lane; no compatibility path is retained. | `903414cc4`; assumed removed |
| Status reporting | 89 | **Mixed:** shared disk-installed facts benefit all five; Show keeps policy/install/serving, provider, and Node projection. Duplicate filesystem inspection becomes **dead**. | `903414cc4`; revalidated `2c6fcbe88` |
| Node version checking | 61 | **Show-specific:** Show executes a JavaScript directory artifact through system Node and enforces its runtime prerequisite. | `903414cc4`; revalidated `2c6fcbe88` |
| npm provider | 37 | **Show-specific:** an explicit operator provider outside the pinned manifest transaction. | `903414cc4`; revalidated `2c6fcbe88` |
| Other | 18 | **Shared/dead:** common redaction, errors, hashing, tagging, env parsing, and sanitizing already have shared owners; Show wrappers disappear. | `903414cc4`; revalidated `2c6fcbe88` |

Tmux is a separately measured **699-line** outlier at `2c6fcbe88`; it was not
part of the supplied 1,621-line Show grouping. It independently owns manifest,
archive, extraction, transaction/persistence, guard, and status mechanics, but
not retention. Its macOS preparation, runnable/compatibility checks, utf8proc,
and terminfo behavior remain product concerns.

## Measured Current-Behavior Census

This section is non-normative. Each row is a falsifiable observation at its
named revision, not a promise about a future implementation.

| Current fact | Observation | Measured at |
| --- | --- | --- |
| **Step 1:** Shared disk readers | `status()` loads a manifest first, and `resolve_binary()` returns `None` before disk resolution when the manifest is unavailable or not installable. | `2c6fcbe88` |
| **Step 1:** Released model-hub claims | `install-state.json` schemas 1 and 2 persist a target containing `manifest_sha256`; recovery compares that target. | `2c6fcbe88` |
| Record discrimination | Current metadata/current-pointer writers persist no `record_schema_version`; released records are therefore unversioned, and field absence carries no record-generation information for a future schema change. | `2c6fcbe88` |
| **Step 1:** Memory development provider | Presence of `AVIBE_MEMORY_DEV_RUNTIME` selects the development branch before managed state. A missing or incompatible configured interpreter returns the development-provider failure and does not fall through to a managed pointer or manifest. | `2c6fcbe88` |
| **Step 1:** Memory activation | Memory overrides pointer writing; with a live provider root it fails closed without the controller coordinator, which receives candidate, root state, commit, and rollback callbacks. | `2c6fcbe88`; rechecked on master 2026-08-23 |
| **Step 1:** Memory admission | A released active pointer without `admission_revision` reruns the runtime compatibility probe. Success admits it; a false result or raised inspection rejects it rather than trusting shared binary metadata. | `2c6fcbe88` |
| **Step 1:** Disk-pointer confinement asymmetry | Baseline shared `_current_install_dir()` resolves `install_dir` and checks only that the versions root is a parent; the in-flight shared reader adds absolute and strict resolution, rejects the versions root itself, and requires the binary beneath `install_dir`. Memory already adds regular-file and `X_OK` checks. | `2c6fcbe88`; Step 1 diff checked 2026-08-23 |
| **Step 1:** Unreadable manifest labels | A configured-local `read_bytes` `OSError` is `manifest_missing` in shared and invalid in Show; shared offline-cache read failure is `manifest_unavailable_offline`. | `2c6fcbe88` |
| Show prerequisite identity | Show install matching compares runtime version, platform, and archive SHA-256, but not `minimum_node`; old metadata continues to supply the old prerequisite after a prerequisite-only manifest edit. | `2c6fcbe88` |
| Tmux schema 1 admission | `_verified_manifest_binary` checks metadata and `_tmux_binary_runnable`; the runnable check accepts any non-empty `tmux -V` result and does not compare it with manifest `tmux_version`. | `2c6fcbe88` |
| Tmux byte phases | Source-leaf verification occurs before preparation; macOS ad-hoc signing can change the installed binary bytes afterward. | `2c6fcbe88` |
| **Step 1:** Online unrelated-platform edit | `_manifest_install_dir()` hashes the whole manifest digest with the selected archive digest. An online manifest edit for another platform therefore selects a new directory even when this host's archive bytes are unchanged. | `2c6fcbe88` |
| **Step 1:** Model-hub platform alias | Model-hub maps host `linux-x64` to released artifact label `linux-amd64`; its released pointer, metadata, and claim can therefore carry a label different from the host tag. | `2c6fcbe88` |
| **Step 4:** Downloads namespace | Remote manifest caches use `downloads/manifest-<digest>.json` beside archives, so the namespace mixes durable manifest facts with disposable archive bytes. | `2c6fcbe88` |
| **Step 4:** Retention with a readable pointer | Shared and Show both protect the pointer's install unconditionally and rank the remaining installs by mtime, so a requested count of zero, one, or two preserves the current install plus exactly that many previous installs whatever the current install's own mtime rank. Show protects one thing more: `_install_dir_overlaps_protected()` matches ancestors and descendants of a protected path, so a legacy install root that contains the current fingerprint directory survives a requested count of zero and is not one of the counted previous installs. Shared protection is exact-membership only, and its consumers' versioned layouts do not nest, so no overlapping install exists on this layer to protect. | `343a6b4a50`; hermetic probes 2026-08-24; `test_show_runtime_clean_skips_legacy_parent_of_current_fingerprint` |
| **Step 4:** Retention after pointer inspection failure | Shared `_current_install_dir()` and Show `_current_manifest_install_dir()` both catch every pointer read/parse/path exception and return `None`, which contributes nothing to the protected set and leaves the live install ranked by mtime alone. With a corrupt, unreadable, or absent pointer both managers plan the live install for deletion at a requested count of zero, and at every requested count once the live install is no longer among the newest that count retains; each case still reports success. Show's archive pass in the same call refuses only for a *present* pointer it cannot read or parse, raising `current.json is unreadable`; an absent pointer is not a refusal there, because `_protected_archive_sha256s()` catches `FileNotFoundError` and keeps collecting protected digests from the retained installs' metadata. | `343a6b4a50`; hermetic probes 2026-08-24 |
| **Step 4:** Downloads reclamation | Shared cleanup enumerates staging directories and versioned installs only. At a requested count of zero, a real clean reclaims no bytes from `downloads/` — a superseded archive, the current archive, an orphaned `.tmp` staging file, and a manifest cache all survive — and the result carries only `ok` and `removed`, reporting neither those artifacts nor any size. Show already reclaims content-addressed archives against a protected digest set and reports counts and bytes. | `343a6b4a50`; hermetic probe 2026-08-24 |
| **Step 4:** Removal failure reporting | Shared `_clean_locked()` deletes each staging directory and versioned install with `shutil.rmtree(..., ignore_errors=True)` and appends the path to `removed` unconditionally, then returns `ok: True`, so a removal blocked by permissions or an in-use file is reported as reclaimed. Show's archive pass instead unlinks each file under its own `except OSError`, increments `failed_count`, adds to `removed_bytes` only after a successful unlink, and reports `outcome: partial` with reason `archive_removal_failed`. | `343a6b4a50` |
| Inspection versus absence | Shared versions-directory preview preserves traversal errors instead of treating them as proof of no install; Show status can still collapse a raised inspection into absent at its consumer boundary. | `2c6fcbe88` |
| **Step 3:** Released Show links | Each v3.0.13 Darwin/Linux archive has 16 symlinks and one esbuild hard link; the four Unix archives have nine forward symlinks total, prior-target hard links, and no finally dangling symlink. Windows archives have no links. | `v3.0.13` manifests and archives; measured 2026-08-23 |
| **Step 3:** Extractor behavior | Shared stops at the first link member. Show and tmux accept the benign regular/symlink/hard-link probe and raise on an escaping `linkname`. | `2c6fcbe88`; hermetic probe 2026-08-23 |
| **Step 3:** Filter capability | The project supports Python 3.10+. Show capability-detects `filter="data"`; shared and tmux use a Python 3.12 version gate even though the filter is backported to 3.10.12 and 3.11.4. | `2c6fcbe88`; Python docs checked 2026-08-23 |

## Step 1 Executable Acceptance

**Intent:** Move installed-state truth for git, Memory, and model-hub from the
selected manifest to admitted disk records and artifacts. Status, resolvers,
and reuse derive installed identity from disk while manifest failure remains a
separate candidate-operation diagnostic; Show and tmux are unchanged.

Every Step 1 shared consumer rejects external or symlink-resolved pointer paths.
Model-hub claim cases cover schemas 1 and 2 and normalize whole-manifest targets for resume/recovery; acceptance verifies each fixture's released version, digest, and source bytes.
The entrypoint universe is generated from the public operation surface exported
by the Step 1 manager and adapter code, with a reflection check that fails when
that surface and its code-owned contract registry differ. The executable
contract is the Cartesian coverage matrix for each fixture's owning adapter;
adding a fixture or public operation creates an uncovered cell until its test
exists. Each adapter has a non-empty fixture set, and every cell asserts its
fixture's output and mutation predicates rather than counting a smoke call.

Two mechanical rules gate Step 1:

1. Before implementation, record and `rg`-verify a non-empty baseline enumeration
   of every production or test symbol scheduled for deletion. Every listed
   symbol has zero definition and call-site hits at the deletion commit.
2. At delivery, record and `rg`-verify a non-empty enumeration of every predicate
   whose text differs between the step's merge base and delivered head, keyed by
   owning symbol and call site. Every listed predicate maps to a retained executable
   predicate or an explicitly approved behavior change; an unaccounted row fails.
   Each retained predicate has a non-zero production call-site count and an
   acceptance test that exercises it.

A step gate is self-checked against its intent: an unchanged-result clause is
valid only for behavior that the intent does not require changing.
Enumeration binds when a gate becomes executable: the in-flight step's allowed
and forbidden sets are author-verified with `rg` as enumerated symbols or census
rows. Later gates state only direction and a stop condition; the PR that makes
one executable inherits its enumeration obligation.
Deferred intents name inherited obligations and required end-state properties;
fixtures, operating-system jobs, and scenario lists remain implementation-time proof methods.

## Migration Sequence And Gates

The following mechanical gates apply to every step:

1. A non-dunder shared hook that this migration introduces, or converts from
   `raise NotImplementedError` to concrete behavior, fails when every on-layer
   consumer overrides it; at least one must inherit it unchanged.
2. A new public shared-layer symbol with zero production call sites fails the
   step. Test-only call sites do not count.
3. The contract author marks each normative row of the Measured
   Current-Behavior Census with its owning step. All and only the census rows
   marked for a step define that step's corpus membership, which
   implementation cannot elect. Every marked row has an executable case. A
   marked row whose own observation cites a released artifact or record
   additionally requires a fixture provenance-verified against the released
   version and digest it derives from. A row citing no released bytes — an
   interpreter capability, a code behavior, or a constructed hostile input —
   has no released fixture to verify and is covered by its executable case
   alone; the row text decides this and implementation never classifies rows.
   An unmarked census row states an observation, so it contains no `MUST`,
   `rejects`, or `fails`; that prohibition is the whole test, because a
   path-naming clause cannot distinguish a production path from a fixture path
   under `rg`. Both halves scope to that census table alone, so the other
   tables in this document keep describing current behavior in their own
   words.

### Step 2: Converge The Mutation Guard

**Intent:** Move the strongest existing guard and preview behavior into the
shared manager so later installer migrations inherit one concurrency owner.

**Gate:** Shared consumers pass focused guard, preview, cleanup, and install
tests with no installed-runtime regression. Lost path confinement, retained
misclassification, or a consumer result change outside the corrections measured
in the **Mutation lock follows/under-validates its path** and **Preview collapses
failure causes** rows stops the sequence before Show uses the guard.

### Step 3: Support Composite Artifacts

**Intent:** Let the shared installer represent Show's released directory
artifact and internal links under the Step 1 disk-state model and Step 2 mutation
guard while retaining the binary-artifact path; reuse capability detection for
tar filtering without raising the Python floor.

**Gate:** Every released v3.0.13 platform archive measured in the census installs
through the shared path and exposes its expected CLI/esbuild entrypoints,
hostile archive probes remain confined, and git/Memory/model-hub fixtures stay
unchanged; failure breaks a supported platform or weakens existing binary
consumers, so the sequence stops before cutover.

**Deferred hardening (non-gating):** both current extractors have an unfiltered
fallback and no explicit final-tree ownership or POSIX-mode normalization.

### Step 4: Add Shared Retention

**Intent:** Give the shared manager one lifecycle for verified downloads,
installed versions, protected rollback state, and cleanup reporting under the
Step 2 guard, exposed through `vibe runtime clean` for every on-layer consumer.

**Gate:** Every marked Step 4 census row has an executable case. For requested
retention counts of zero, one, and more than one, dry-run and real cleanup
preserve the current install plus exactly that many eligible previous installs,
where an install overlapping a protected path is protected rather than counted.
The shared consumers' layouts do not nest, so that exclusion selects nothing on
this layer today; it is stated so the count cannot later be read as license to
delete an overlapping compatibility root once Show's nested legacy layout
arrives here at Step 5. Any pointer that is present but does not resolve to a
valid confined current install is an inspection failure that plans no deletion
and reports that failure rather than reporting success — unreadable,
unparseable, structurally wrong (a non-object payload, or an `install_dir` that
is missing, not a string, empty, or relative), or naming a path outside this
runtime's versions directory. Only a genuinely absent pointer means nothing
claims to be current, and even then cleanup does not delete an install this
manager's own resolver would still admit under the Step 1 disk-state model.
Protection of the live install comes from that ruling and never from its mtime
rank, so a requested count of zero protects it as strongly as any other count.
Superseded archives and orphaned staging files become reclaimable and appear in
the cleanup report, manifest-cache facts remain usable offline, and cleanup
failure does not overturn a committed install. `vibe runtime clean` invokes
cleanup for each of git, Memory, and model-hub, not git alone, and a failure
reported by any one of them makes the command's own result a failure with a
nonzero exit status while each runtime's own report survives intact; any
git/Memory/model-hub loss stops rollout for that dependency. A removal that did
not happen never appears in the report as removed: real cleanup detects each
failed removal rather than delegating it to `ignore_errors`, counts it, and
surfaces it in that runtime's own report before the aggregate result is
computed. Show's archive pass — per-file `except OSError`, a `failed_count`, and
a partial outcome — is again the precedent this adopts rather than a new policy.

**Out of scope for this step:** automatic post-install retention. The step
delivers the command, so `ensure()` gaining its own cleanup call is a separate
decision and not a gate item.

**Sequencing within the step:** the protected-set correction lands before any
reclamation is added, because reclamation widens what an empty protected set can
delete. Where a bound stays imprecise, cleanup errs toward not deleting: an
uncleaned directory costs disk space, while a deleted live install costs a
redownload the user cannot undo.

Show carries the same pointer-inspection defect today and is not patched
separately for it: Step 6 deletes the Show cleanup that holds it, so a Show-side
fix would be discarded at cutover, and the exposure until then is a redownload
of a re-obtainable runtime behind a damaged pointer. Show's archive pass is the
precedent the shared ruling adopts rather than a new policy.

**Retention here is bounded by count, not by reference, and this step does not
change that.** `show-runtime-availability.md` records that a cached
`_managed_command` can still name an install directory after the pointer
advances, that the runtime `require()`s modules lazily for its whole lifetime,
and that `keep_previous` therefore bounds retention by count rather than by
reference; W6 owns closing it. Show's own cleanup carries the identical count
bound today, so putting Show on this layer at Step 5 neither closes nor widens
the exposure, and no gate above should be read as closing it. The one bound this
step does owe is negative: the reclamation it adds targets archives under
`downloads/` and `install-*` staging directories, which no handed-out command
names, so it must not extend the exposure from install directories to newly
reclaimed bytes.

### Step 5: Cut Over Show's Manifest Provider

**Intent:** Compose Show's manifest provider as a full consumer of Steps 1-4 while
leaving direct archive, npm, Node policy, availability, and serving behavior in
Show; align unreadable-source diagnostics and admission-relevant manifest changes
during that measured cutover.

**Gate:** Released and canonical Show fixtures cover online, offline, provider,
layout, Node, Doctor, dependency-status, and composite-archive flows with one
declarative manifest installer owner; any released install that disappears,
redownloads, or cannot serve on its previously supported platform stops deletion.

### Step 6: Delete Show's Parallel Installer

**Intent:** Remove unreachable Show manifest installation, cache, guard,
cleanup, persistence, and utility ownership while retaining product adapters.

**Gate:** Deleted Show symbols have zero definitions and call-site hits in
source and tests, focused suites for the four migrated consumers pass, and no
private compatibility shim recreates a second installer; tmux remains the one
temporary outlier.

### Step 7: Migrate Tmux And Enforce Sole Ownership

**Intent:** Move tmux onto the proven shared installer after Show as a full
consumer of Steps 1-4, retaining its released-state reader, macOS preparation,
and runtime compatibility adapter.

**Gate:** Released tmux inputs and prepared-binary tests pass through the shared
path and the other four dependencies remain unchanged. The non-empty obsolete
adapter-owned installer-symbol sets recorded at the Step 6 and Step 7 baselines
have zero definitions and call-site hits; the end state has one declarative
manifest installer owner for all five dependencies.

## Measured Size And Concept Estimate

All rows below are non-normative planning measurements. Function spans use
`end_lineno - lineno + 1`; imports, declarations, blank lines outside
functions, serving-only functions, and tests are excluded.

| Measurement | Value | Measured at |
| --- | ---: | --- |
| Show file / tmux file / combined outliers | 3,370 / 699 / **4,069 physical lines** | `2c6fcbe88` |
| Show install roots before GitHub-provider removal | 110 functions / 2,579 span lines | `903414cc4`; revalidated `2c6fcbe88` |
| GitHub-only reachable helpers | 11 functions / 212 span lines | `903414cc4` |
| Show install roots after that prerequisite | 99 functions / 2,367 span lines | derived from the same audit |
| The 24 same-name pairs | 561 Show span lines | `2c6fcbe88` |
| Supplied uncounterparted grouping | 1,621 Show lines | supplied audit at `903414cc4` |
| Tmux functions | 38 functions / 568 span lines | `2c6fcbe88` |

The historical implementation estimate, measured before later seams were
discovered, is retained only to answer the sizing question; it is not
maintained as future steps are designed.

| Historical estimate | Show / Steps 1-6 | Tmux / Step 7 | Total | Measured at |
| --- | ---: | ---: | ---: | --- |
| Lines deleted from outlier modules | 1,560-1,650 | 489-539 | **2,049-2,189** | `2c6fcbe88` planning model |
| Existing behavior moved by ownership | 732 | 451 | **1,183** | `2c6fcbe88` planning model |
| Lines added to shared layer | 510-610 | 20-50 | **530-660** | `2c6fcbe88` planning model |
| Net production reduction | 950-1,140 | 439-519 | **1,389-1,659** | `2c6fcbe88` planning model |

The original owner-instance model counted seven common capabilities
(primitives/extraction, manifest selection, archive acquisition/cache, install
transaction/persistence, mutation/preview guard, retention/cleanup, and
status/probe), four Show product concepts, and two tmux product concepts. Its
historical forecast was **26 concepts before and 13 after**, measured at
`2c6fcbe88`. Later discovery of Memory's development-provider and coordinated
activation seams makes that arrow non-exhaustive; it is not recalculated or
used as acceptance evidence. The retained product inventory includes those two
Memory seams alongside the four Show and two tmux concerns named above.

A surviving duplicate owner is a defect; missing a forecast range is not.

## Residual Unknown

Detailed contracts for Steps 2-7 are deliberately deferred, not silently
assumed. Each is written from the measured output of its predecessor. The prior
Python-floor question is resolved: keep `requires-python >=3.10` and prefer the
existing capability-detection pattern. Step 3 gates path/link confinement;
final-tree mode/ownership alignment remains deferred hardening. There is no
remaining archive-link or Python-release decision in this document.

## Non-Goals

- No production code or test change in this PR.
- No change to Show serving or request behavior.
- No resurrection of the removed GitHub provider.
- No attempt to force direct archive or npm into the manifest security model.
- No deletion of released compatibility readers by this document.
- No issue filing and no dependency addition.
