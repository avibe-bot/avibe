# Avibe Release Workflow

This document contains the release-specific details that are too operational
for `AGENTS.md`. The workflows are the source of truth for the final commands.

## Select the release commit

Fetch `origin/master` and record the candidate's exact SHA for all release-only
validation and tag creation; do not switch it silently while checks run. New PRs
may continue to merge: there is no routine release-time merge freeze.

`release_ai.yml`, `publish.yml`, and `desktop-release.yml` each start with the
release gate, `scripts/ci_release_backstop.py`, before any build, signing, or
publication path. It resolves the tag to its exact source SHA and requires
proof about that SHA. The proof depends on the tag.

### Stable and every other tag: the full master backstop

Require a successful **full master backstop at the exact candidate SHA**.
`lint.yml` runs that full pass after master pushes, including iOS, desktop,
browser, installer, web-fetch and Windows checks. A running pass finishes; only
the newest pending master pass waits. See
[CI scope and routing](plans/ci-affected-routing.md). The PR-only iOS
NavigationPolicyTests quarantine (#3418) does not relax this release gate:
iOS must pass in the exact full master run, even while its PR check is advisory.

A green PR head, another SHA's full run, or a missing/coalesced run is not
proof. Select a full-green master SHA; wait for its current full pass, or rerun
its existing master backstop. Manual full validation is available on master.
Historical runs with no explicit full-backstop certificate are not assumed
equivalent.

This is the gate for a stable `v*` tag in Stage 1, `Publish to PyPI` and
Stage 2, and for every tag that is not a canonical `gh-vX.Y.ZrcN`. `Publish to
PyPI` applies it to any tag it is given.

### GitHub-only RC tags: the merged pull request

A `gh-vX.Y.ZrcN` tag does not wait for the full backstop. Stage 1 and Stage 2
require instead that:

1. the tag's SHA is on `master` and is the merge commit of a pull request
   merged into `master`; and
2. on that pull request's head, every check `master`'s ruleset requires
   concluded `success` or `skipped`. Where a check ran more than once on the
   head, its newest run counts, as it did for the merge.

The gate fails closed. It refuses an RC whose SHA resolves to no such pull
request, such as a direct push or a commit that is not the merge commit; one
with a required check that failed, was cancelled, is still running or is
absent; and one it cannot prove because GitHub could not be read or `master`
has no required checks. Nothing after the gate changes for an RC: the exact
tag/source binding, the predecessor contract, immutability, signing,
notarization and the mirror apply as before.

The full backstop still runs on `master`, and a red one still gets its
fix-forward owner. For an RC the gate only reports the state at the RC's SHA:
`success`, `pending`, `failed`, or `missing` when no full pass finished there
because it was coalesced away, cancelled or never started. A `failed` state is
a warning on the release run, not a refusal.

**The release Session owns the follow-up.** After publishing an RC it follows
the full pass at the RC's SHA to its conclusion, or the next full pass on
`master` when that one was coalesced away. If it ends red, the Session reports
the RC tag, the SHA and the failed jobs to the product Bot (`product`). The RC
stays published, and no stable is cut from a SHA without its own green
backstop. To read the state again:

```bash
GH_TOKEN="$(gh auth token)" python3 scripts/ci_release_backstop.py \
  --repository avibe-bot/avibe --tag <rc-tag> --rc-pull-request-gate
```

### Before pushing the tag

Fetch master again. If the release is required to include newly landed work,
select the new tip and repeat candidate-specific gates; otherwise retain the
recorded, validated candidate. Require the owner's release authorization
in either case. The stable-release E2E below and asset-complete publication gates
remain mandatory.

## Tags and notifications

- Tags advance the latest version number, for example `v1.0.1` to `v1.0.2`.
- For an official `v*` release, the annotated tag is the only release-state
  input. Put `<!-- avibe:update-notification=none -->` in the tag annotation
  when the release should suppress update and post-update notifications while
  automatic updates remain enabled.
- The marker also silences the desktop app: its automatic startup check does
  not prompt for a silent release, which still shows as available and is
  offered by an explicit check or a remote install request. The shell reads
  the marker from the mirror index's `update_notification` field, or from the
  release notes when it falls back to the GitHub API. Desktop apps built
  before this rule still prompt.
- Push the tag and let the official workflows create the release. Do not
  pre-create or manually edit a GitHub Release.
- The workflow emits both current and legacy `vibe-remote` update markers for
  installed-client compatibility.

## Workflow ownership and artifacts

The desktop packaging cache is intentionally separate from release correctness.
`desktop-package.yml` has one writer: the unsigned `master` warm-up. It builds
all three targets, caches only third-party Cargo output, and keys Rust entries
by `Cargo.lock` checksums, rustc, Apple SDK, and target. First-party crates,
version stamping, the private Runtime, final packaging, signing, notarization,
and verification are rebuilt; release jobs restore only an exact key and visibly
warn before rebuilding on a miss. The Cua Driver cache remains provenance- and
input-verified. PR desktop-shell jobs are restore-only, and maintenance keeps
one newest release entry per target, removes superseded entries and retired PR
caches, reports total usage/headroom against the 10 GB repository budget, and
retries a failed master warm-up once. The existing master warm-up remains the
cache writer for changed desktop inputs and its existing schedule; no additional
five-day warm-up was added. Instead, `desktop-cache-keepalive.yml` is the
retention mechanism and runs lookup-only on master pushes and every five days.
A missing, unreadable, or unrefreshed entry emits an explicit instruction to run
the next warm-up; it never silently dispatches one.

A TEST RC deliberately builds Apple silicon macOS and Windows only. Stable
still requires Apple silicon, Intel macOS, and Windows with the complete signed
set. The unsigned master warm-up is the early Intel signal for desktop source,
locked dependency, and release-helper changes plus its existing schedule; it
catches Intel compile/SDK/smoke/DMG failures before stable. Intel-specific
Developer ID signing, notarization, stapling, Gatekeeper, and updater-signature
failures still first appear on the stable path. The keepalive's lookup-only
behavior is an observation with warnings, not a release correctness gate.

The official workflows stage notes and release assets in one immutable tag/release.
`Release (AI Notes)` generates notes and publishes the core/runtime assets for
GitHub-only pre-releases; for official stable tags it leaves a Draft for
`Publish to PyPI`, which remains the stable core/PyPI finalizer after exact
notes verification. `Desktop Release (Stage 2)` runs independently after the
core stage, preserves the exact tag/source binding, and attaches the verified
signed desktop set to the same release. The updater and mirror each treat a
partial desktop set as unavailable until Stage 2 completes.

The two release workflows may reuse a canonical Show Runtime bundle set from a
previous successful release run when every identity field matches: trusted
producer workflow/run, resolved `package-lock.json` digest, Node/npm toolchain,
platform, external Runtime ref, exported router digest, and archive digest. A
missing or mismatched identity falls back to a native rebuild and publishes a
new canonical set. This applies only to the unsigned Show Runtime archives and
router; release-versioned wheels, desktop-private wheels, and signed assets are
never reused.

For GitHub-only RCs, canonical discovery inspects the newest successful runs
first and stops at the newest complete set. Only same-repository `push` and
`workflow_dispatch` producers are trusted; a PR/fork run cannot supply release
bytes even if it uploads matching names and identity records. One Linux job downloads and verifies
all six platforms against the pinned native Node/npm versions; it does not run
foreign binaries. Any missing or invalid member sends all platforms through the
native build matrix. Stable `publish.yml` retains its native matrix path.

Core packaging may reuse `ui-dist-linux` only when the gate already supplied a
successful full master backstop proof for this exact source. The consumer binds
the artifact to that run/attempt's successful build job and verifies the ZIP's
GitHub SHA256 digest before extraction. No proof means no artifact-discovery
request; missing, expired or mismatched bytes use the normal local UI build.
A partial CI rerun that inherits an earlier attempt's successful UI build or
certificate also falls back: the producer job must have actually started in the
proven attempt, not merely carry its relabelled attempt number.
This optional optimization never makes the full backstop an RC gate again.

The final wheel/sdist are still built with the release version and activated
predecessor compatibility. All distribution assertions remain: the three fresh
full-dependency installation checks run concurrently in separate processes,
temporary homes and virtual environments, not shared installed environments.

The RC publisher prepares notes after the gate while packaging runs, then
waits for `build-assets` in the **same workflow run and attempt** to succeed.
It verifies and downloads that attempt's immutable artifact and rechecks the
remote tag before publication. Failure, cancellation, stale artifacts, or a
30-minute wait timeout cannot publish. To retry this path, rerun all jobs;
reusing a previous attempt's successful build is intentionally rejected. Only
the publisher has write permissions. Preparing it early consumes runner time
while it waits; desktop signing and Stage 2 are unchanged.

GitHub-only pre-releases use the `gh-vX.Y.ZrcN` format, such as
`gh-v2.2.8rc2`, so they remain distinct from PyPI-triggering `v*` tags. They
must include an installable wheel containing `ui/dist` and
`vibe/show_runtime/*.tgz`, plus the source distribution.

## Direct-transport sidecar artifacts

`direct-transport-v<VERSION>-<N>` tags are auxiliary five-platform sidecar
releases, not Avibe application updates or Apple XCFramework releases. The
[publication contract](plans/direct-transport-publication.md) owns exact-source
qualification, redistribution notices, explicit non-latest publication, public
byte readback and separate consumer-pin promotion. Only the guarded manual
workflow publishes; merging its code does not authorize dispatch or enablement.

## Python dependency artifacts

Every new release ships `dependency-constraints.txt` as a GitHub asset and the
same content as `vibe/dependency_constraints.txt` inside its wheel and sdist.
Regenerate it after updating `uv.lock` with
`python scripts/release_dependency_constraints.py`; `--check` verifies freshness.

Release builds retain exact transitive pins in distribution metadata, including
platform/Python markers, so older installers such as 3.2.5 also obey the release
graph. Source/editable development keeps its declared ranges.

The shell installer and `vibe upgrade` inspect the target wheel before installing
and pass its own constraints, not those of the running release. The shell's
stdlib resolver is generated from `vibe/release_dependencies.py`; after changing
it run `python scripts/sync_install_dependencies.py` (`--check` detects drift).
Explicit old stable artifacts through 3.2.5 remain installable without a lock.

Exported hashes are provided for verification, but uv tool installation does
**not** enforce dependency hashes from constraints. Exact versions do not remove
trust in the selected package index or pin source-build dependencies. The default
installer no longer silently retries a third-party Python index or Git HEAD.
Avibe's release mirror can provide the core wheel; it does not mirror the full
Python dependency graph. Where PyPI is unreachable, configure a trusted HTTPS
proxy or explicitly select an index whose artifacts you trust. See the change
contract in [release dependency locks](plans/release-dependency-locks.md).

## Python distribution descriptions

The existing Hatch hook generates the PyPI description from `README.md`, making
relative images and file links absolute without changing the repository README.
Both release build workflows pass their resolved tag as `AVIBE_BUILD_TAG` alongside
the package-version override. The hook checks that tag against the package version
and uses it for version-frozen image and file URLs, including `gh-v*` previews.
An exact matching local Git tag also works; other development builds use `master`.
The sdist keeps the original `README.md` and archives the resolved description in
its static project metadata, so rebuilding a wheel without Git or release
environment preserves the same description. No extra build dependency is needed.

## Activate a release on a machine

Install a release, RC or stable, only from the published GitHub Release, never
from a workflow artifact or a Draft. The wheel's Show Runtime manifest points at
that tag's release assets, and an unpublished release serves them to nobody, so
the new service fails to download its runtime and Show pages fall back. Before
installing, require that the release reports `isDraft=false` and that a runtime
archive downloads without authentication, for example
`curl -fsSL -o /dev/null https://github.com/avibe-bot/avibe/releases/download/<tag>/vibe-show-runtime-node-darwin-arm64.tgz`.
An authenticated `gh release download` is not proof: it also reads Drafts.

Install through the tagged `install.sh` with `AVIBE_INSTALL_PACKAGE_SPEC` set to
the published wheel, and do not skip its Show Runtime preparation. After the
restart, open an existing Show page and require real page content: an
`X-Avibe-Show-Recovery` header means the fallback answered, even with HTTP 200.

```bash
curl -fsS -D - -o /dev/null "$BASE/show/$SESSION/" |
  python3 -c 'import sys; h=sys.stdin.read().lower(); assert "x-avibe-show-recovery:" not in h, h'
```

Then load that page's HTML and its scripts, not a redirect or a login page.

## Release E2E

Pull-request CI does not run the hermetic Model Hub E2E (`-m e2e_model_hub`).
`Model Hub release E2E` runs it nightly and on demand on clean runners, with
the pinned engine and the latest agent CLIs. Before tagging a stable `v*`
release, dispatch it on the release commit and require a green run at that
exact SHA; a strict `xfail` names a known, tracked gap, not a pass to ignore.

The run shares the selection out over two shard jobs, and its `model-hub-e2e`
job is the verdict. That job passes only when both shards succeeded in the
same run attempt at the release commit, against the same agent CLI versions
and the engine that commit pins; when together they ran every case of
`-m e2e_model_hub tests/e2e` exactly once; and when every case a covered
scenario row cites passed. Shards from different attempts do not join: after
a failure, re-run all jobs, not only the failed one.

## Model Hub engine channel

The engine channel lets an installed Avibe adopt a newer Model Hub engine
without an Avibe release. Its design, trust chain and client rules are in
`docs/plans/model-hub-engine-channel.md`. This section is the release side.

**The channel is not commissioned.** Avibe ships no channel key
(`vibe/model_hub_runtime/engine_channel_roots.json` has no root), so installed
clients make no channel request and every publication step stops first. No
installed Avibe adopts an engine from this public channel yet.
Commissioning is listed at the end of this section and needs the owner.

### One signed index, one ledger

`https://dl.avibe.bot/index/model-hub-engine.json` tells clients which engine
releases may be adopted and which were withdrawn. It is a signed envelope
around an exact payload; each release entry carries the minimum Avibe version
and the exact bytes of that release's manifest. Clients trust it only through
a key shipped inside Avibe, never because of where it was downloaded.

There is one current index. It has no expiry and is never renewed: a client
follows the highest sequence it has accepted for as long as that is the
highest it has, and only a promotion or a withdrawal produces a new one.

The exact bytes of each allocation are committed by reviewed PR, before signing.
Only the current protected `master` snapshot authorizes publication. Artifacts,
previous runs and the public object do not authorize a signature.

Nothing regenerates the index from the list of GitHub releases, and
`Mirror releases to dl.avibe.bot` refuses to write or delete that object.

### From candidate to adoption

1. **Record.** Review a candidate record under
   `patches/model-hub-engine-candidates/` (format in its `README.md`): the exact
   six-asset manifest and the minimum Avibe version. The record is separate
   from the packaged engine, so merging it changes nothing users install.
2. **Rehearse.** Dispatch `Qualify Model Hub engine candidate` on `master` with
   the record's tag. It holds no business secret, repository-write token or
   OIDC publication authority. Its disposable jobs rebuild and exercise the
   exact candidate and publish only the informational result named
   `untrusted_rehearsal`, with source, manifest/archive digests, run identity,
   and missing/failed observations. It does not authorize anything.
3. **Prepare and publish.** Dispatch the channel workflow with
   `operation: release` and the rehearsal run id. Its trusted prepare job uses
   a fixed checkout, checks the exact run and current candidate/source/manifest/
   archive digests, parses the result as bounded untrusted data, and places
   only byte-checked assets in a separate trusted artifact namespace. The
   existing protected writer environment then requires explicit owner approval
   for this run and digest before publishing immutable, non-latest assets.
4. **Promote.** After the mirror holds the release, merge the reviewed allocation PR. The PR preflight verifies the designated rehearsal and published bytes before merge. Asset publication alone never changes the index.

The trusted prepare job verifies the workflow run is the intended dispatch on
`master`, the run attempt and commit, the current reviewed candidate, the
upstream source reference, exact manifest/archive digests, minimum Avibe and
the concrete baseline context. It independently resolves the current minimum
and stable Avibe releases, verifies immutable wheel sizes and SHA-256 digests,
and reads their packaged manifests as ZIP data; it does not trust released-role
or bundled-manifest files from the rehearsal artifact. It never executes
candidate code, imports uploaded modules or wheels, or accepts an uploaded
executable as trusted input. Runtime pass/fail assertions remain explicitly
untrusted. The job split protects the
trusted workflow and privileged credentials; it does not prove that a
malicious candidate reported honestly. A future stronger receipt or
malicious-candidate-resistant provenance requires a new product/owner decision.

| Observation | What it shows | Current producer |
| --- | --- | --- |
| `build` | The pinned source reports a rebuild matching the reviewed manifest and six assets. | Yes; independently rechecked by trusted prepare |
| `platform_smoke` | Each archive reports startup and Avibe health on its own platform. | Yes; untrusted runtime data |
| `hosted_wire` | The hosted passthrough comparison with each baseline engine. Reviewed differences remain visible. | Yes when a candidate has baselines |
| `released_avibe_e2e` | The hermetic E2E through the minimum and current-stable released wheels. | Yes when immutable wheels are available |
| `backward_state` | Candidate state read back by baseline engines. | Future lifecycle work; missing is reported, not fabricated |
| `lifecycle` | Activation, rollback and recovery observations. | Future lifecycle work; missing is reported, not fabricated |

All concrete integrity, manifest, minimum-version, availability, health and
rollback checks remain useful data or trusted checks at their owning boundary.
No universal failure is caused solely by absent backward-state or lifecycle
producers. Full candidate containment is rejected for this scope, not deferred
as a prerequisite. PR4 must not restore the removed producer/receipt gate under
another name.

The released-Avibe job installs the published stable wheel into its own
environment and runs **that release tag's own tests** from a directory that
holds no product package. The service starts from the wheel's own entry
script. Every Python process in that environment logs where it imported each
Avibe module from, and the run is refused if any module, in the test runner,
the service or a helper, came from anywhere else.

`Rehearse the released-wheel Model Hub E2E` runs the same shared steps against
one pinned release candidate, so the harness is exercised before the stable
tag freezes its tests. A rehearsal qualifies nothing; its result is separate
from `untrusted_rehearsal` and no owner approval can be inferred from it.

### Operations

Asset publication is independent. Promotion and withdrawal each take one allocation PR; normal delivery needs no second PR or approval. If signing, publication or public readback is interrupted, dispatch the index workflow on master without inputs. It targets current master, not an earlier run or ledger revision.

**What a client without the newest index does.** It keeps following the index
it has. If that index lists a release that a later index withdrew, the client
may still install it, a month later as well as a minute later, for as long as
it cannot fetch the later index and the release's pinned archive can still be
downloaded. A signature shows an index was authorized, and the sequence rule
stops a client going back from one it has seen; neither shows that a newer
one was received. A client that has the later index refuses the earlier one.
This is the chosen behaviour, not a gap to close with a time limit
(`docs/plans/model-hub-engine-channel.md`, section 2). A withdrawal reaches a
machine when that machine next reaches the index.

A withdrawal is permanent. Later indexes carry it unchanged, clients refuse
an index that drops it, and the writer refuses to publish one. The release's
assets and its index entry stay. The fallback must be a listed release that
was not withdrawn and that does not need a newer Avibe than the withdrawn
one. An engine that only a packaged Avibe bundles can be withdrawn too.
Clients learn of a withdrawal at their next check; a machine that cannot
reach the index learns of it when it can.

### Availability of listed releases

`Guard Model Hub engine release` keeps available the engine Avibe packages and
every release the index lists, withdrawn ones and rollback targets included.
On its schedule it verifies each release's published assets against the
manifest, keeps a verified backup, and restores assets a release lost from
the newest backup that still verifies. It uploads only asset names that are
missing and never overwrites published bytes. It reads the index through the
keys Avibe ships and holds no channel key and no bucket credential, so it can
restore pinned bytes but cannot promote a release or undo a withdrawal.

### Hosted signer and writer

The index workflow is disabled until commissioning. Update
`patches/model-hub-engine-channel-ledger/current.payload.json` with the complete desired
index. Commit canonical JSON (sorted keys, compact separators, ASCII, no extra newline),
a frozen `issued_at`, and a strictly higher sequence for any changed bytes, even if the
previous snapshot never reached the origin. Protect master against force-push and require
the preflight on the proposed merge tree. The signer also checks the sequence against
master's Git revisions so a stale preflight cannot spend a sequence twice.

For a promotion, include the designated rehearsal and bind its candidate, snapshot and
base bytes as described in the PR template. Trusted preflight checks the rehearsal,
released wheels, manifests and actual assets. It checks both download sources for every
release the snapshot still offers, including a withdrawal's compatible fallback.
It does not fetch an archive simply to stop offering it. An unpublished candidate can be
removed. A published release must retain its identity or be explicitly withdrawn with a
permanent tombstone. Temporary unlisting is unsupported: omission rejects the snapshot;
it does not cause an installed client to roll back.

Before signing, a job without release credentials checks both download sources again for
all installable assets, including fallbacks. The pinned, isolated signer may sign the
same canonical payload again; this produces the same envelope bytes. Only the master
writer upload step holds the mirror credential. It authenticates the artifact and origin.
An identical payload at the origin skips the PUT. Otherwise the writer rejects a stale
or conflicting sequence, validates published identities and permanent withdrawals,
rechecks installable assets and current master, then creates or replaces with an ETag
condition. The public URL is used only for final readback. A denied read, stale sequence, conflicting payload, failed CAS
or pending public readback is not a successful publication. Dispatch again on master;
there is no historical replay or recovery PR.

**Trust boundary:** anyone who can change the master workflows is trusted with signing.
These checks prevent accidental publication through the standard workflow; they cannot
isolate its authors. A compromised signing key requires replacing the client trust root.
Only the writer upload step uses the existing mirror repository credentials. Those
credentials have bucket-wide write access: leakage can damage other releases, including
the application update index and wheels, but cannot forge an engine signature. The
exact object key is a client restriction, not a server-side permission. The ordinary
release mirror's index exclusion is unchanged.

### Commissioning (owner-held, not done)

Production key generation is a separate owner operation **after the trusted signer
has merged** and the release lead has notified Product. This PR does not generate a
production key, set credentials, ship a root, enable the workflow or publish an index.

The owner generates a dedicated fresh key using the reviewed hosted helper
(`generate`, an explicit `--avault` executable and a new private `--output` file).
The generator needs avault 0.1.11 or later; the CI exercise uses 0.1.11 and the
hosted signer and oldest client verifier use the pinned 0.1.10 binary. Only the
compressed public key is printed. At commissioning, the owner must verify that signing material exists only in the signer environment, restricted to the master branch, with no same-name repository or organization fallback. No manual environment reviewer is required for index publication. Restrict the separate index writer environment to master; do not weaken the asset-release writer's existing approval policy. The index uses the existing mirror credential and requires no new read token. Referencing an environment in YAML does not establish that these deployment settings are present.
The private runbook holds the exact setup commands. Only the public key ID,
compressed public key and genesis sequence floor belong in the root PR.

The isolated contract test verifies disposable key generation, import, signature
verification and deterministic re-signing; it never exercises a production key or bucket.

## Managed-runtime manifests

Published managed-runtime manifests are availability contracts. Keep their
release URLs under the scheduled manifest-verified backup/recovery guard.
Publish replacement assets before changing a pinned manifest so the guard never
restores bytes from a different release. A manifest that pins a third-party
release that Avibe cannot republish (ripgrep, cloudflared) relies on the download mirror as
that guard: the mirror holds the pinned release, verified against the manifest.

## Download mirror

`Mirror releases to dl.avibe.bot` copies published release assets to
`https://dl.avibe.bot/releases/<tag>/<asset>` and writes the release index at
`https://dl.avibe.bot/index/releases.json`. It follows `Publish to PyPI` and
`Release (AI Notes)` via `workflow_run`. `Desktop Release (Stage 2)` explicitly
dispatches the mirror on `master` with `dry_run=false` after successfully
attaching its verified assets; this does not depend on a downstream event from
the token-dispatched desktop workflow. A rejected dispatch fails Stage 2
visibly; recover by dispatching `release-mirror.yml` on `master` with
`dry_run=false`, without rebuilding the signed installers. Dispatch acceptance
is not mirror completion: require the mirror run to succeed and verify the
published download URLs. Either stage can reconcile the same release without
overwriting immutable bytes. It also keeps
version-less links to the newest stable desktop installers, such as
`https://dl.avibe.bot/latest/Avibe_aarch64-apple-darwin.dmg`. The contract and retention rules are in
`docs/plans/release-download-mirror.md`.
