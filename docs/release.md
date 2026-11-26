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

This is the gate for every release tag, stable `v*` and GitHub-only
`gh-vX.Y.ZrcN` alike, in Stage 1, Stage 2 and `Publish to PyPI` as defined on
current `master` (older commits' Stage 1 still accepts the RC pull-request gate;
see below). An RC is cut
only from a SHA whose own full `lint` run on `master` (a push run, or a manual
full run at that exact SHA) completed green. A parent's or a later SHA's green
run does not count. If the SHA's run is red or cancelled, rerun that same run or
fix forward; if it never started (coalesced away) and `master` has moved on, a
run dispatched now would prove a different SHA, so select another candidate
whose own full run is green instead.

Ordinary pull requests no longer run the native and browser legs, so a green
pull request is not full proof and the pipeline does not accept one for an RC.
A tag-push run uses the workflow files at the tagged commit, so a tag on a
commit from before this rule would still run the older, PR-gate Stage 1. The
pre-tag check below is therefore mandatory for every tag, not a convenience
(the one RC exception below replaces it with the operator's hand check, but
only for tags on commits that carry this rule; an older commit's tag-push
Stage 1 still accepts the PR gate, so it always needs the full check):

```bash
GH_TOKEN="$(gh auth token)" python3 scripts/ci_release_backstop.py \
  --repository avibe-bot/avibe --source-sha <sha>
```

**The one RC exception (Product's plain override).** It is an accepted risk,
not evidence. When exactly one full-backstop job never got a runner, an RC may
still ship with Product's go:

1. Before using it, the operator confirms by hand, on the tag SHA's own full
   run, that the named job is the only one not green and that it never started
   (queued with no runner, or cancelled before a runner took it). The script
   does not prove this; the operator attests it.
2. Dispatch `Release (AI Notes)` for the RC tag with `unverified_job` (the one
   job name), `unverified_reason` and `unverified_go` (Product's go, quoted;
   the operator attests that the quote is hers). All three are required
   together, each one plain line without backticks or angle brackets. A
   stable tag or PyPI never
   accepts them.
3. The gate still reads the backstop and prints its full report in the run
   summary (green included), but the attestation replaces its verdict. The run summary and the
   RC's notes print
   `` not verified in this RC: `<job>` — `<reason>` (go: `<quote>`) ``, every
   field in a code span so it renders literally. In the notes it is its own
   paragraph ahead of the generated text, followed by a hidden marker that
   binds the line to the tag's source.
4. Desktop Stage 2 ignores dispatch inputs for this. When the full backstop is
   not green on an RC, it honours an exception only if this exact tag's
   published notes carry that one line with Stage 1's marker for the same
   source and line digest. Prose that merely quotes the format does not count.
   The marker guards against accidental prose, not against people who can
   edit release notes: they can also dispatch Stage 1, so they are trusted as
   release operators.

There is no recheck at publication. If the named job later runs and fails, that
is fixed forward in the next RC.

`ci_release_backstop.py` still has an `--rc-pull-request-gate` option, which
reports whether an RC tag's merged pull request passed `master`'s required
checks. No release workflow on current `master` passes it; Stage 1 as defined
on commits before this rule still does, which the mandatory pre-tag check above
covers.

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
  for a silent release: no notification announces it and nothing installs it
  automatically.
- On the Runtime, the periodic check neither notifies about nor automatically
  installs a silent release. It skips releases the mirror index marks silent
  and still finds the newest announced release below them. A release the index
  does not list yet (a stable release is indexed once its assets are mirrored)
  counts as announced only after its release notes are read without the
  marker; until then the check waits. The unattended install takes exactly
  the release the check found. An admin's own action (`vibe check-update`,
  `vibe upgrade`, a manager's check in the Web update panel or in the desktop
  app's panel for the instance, an update notification's button) still sees
  a silent release. Unprompted version reads (`GET /api/version`, which the
  Web badge and those panels poll) offer only releases the mirror index lists
  as announced, and nothing while the index cannot be read. A check's offer
  stays on screen until the next check, a newer announced offer, a channel
  change, or a version change, and Update installs exactly the version shown.
- The marker also silences the desktop app: its automatic checks (the startup
  check and the auto-update tick) skip a silent release, so they neither offer
  nor install it, while still finding any newer announced release. An explicit
  check, a channel switch, or a remote check still finds and offers it, and a
  remote install request that names it installs it. The shell reads the
  marker from the mirror index's `update_notification` field, or from the
  release notes when it falls back to the GitHub API. Desktop apps built
  before this rule still offer, and with automatic updates on install, a
  silent release.
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
This optional optimization only reuses a build; the full backstop at the exact SHA remains the gate.

The final wheel/sdist are still built with the release version and activated
predecessor compatibility. All distribution assertions remain: the three fresh
full-dependency installation checks run concurrently in separate processes,
temporary homes and virtual environments, not shared installed environments.

The RC publisher starts only after `build-assets` has finished successfully; queued builds do not occupy a publishing runner or start an artifact-wait timer. It then prepares notes, verifies the producer and immutable artifact in the **same workflow run and attempt**, and rechecks the remote tag. A failed, cancelled, missing or inherited build cannot publish. To retry, rerun all jobs, not just the publisher. Stable notes still run when the RC-only build is skipped. Only the publisher has write permissions; Stage 2 already waits for its desktop build and signing jobs by dependency.

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
installed Avibe adopts an engine from the channel yet: the client reads and
checks the index, and the code that would act on it is later work.
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

What the index should say is recorded in a **ledger on the signer host**: the
next sequence, the exact payload last allocated, and every release identity
and withdrawal ever allocated. `Publish Model Hub engine channel` is the
index's only writer, and it plans every change from that ledger. It never
reads the published object, the bucket or a workflow artifact as the state to
continue from. If the published index is replaced by an older signed one, or
deleted, the next publication puts back what the ledger holds, and the signer
is never led to sign the older state again.

The ledger must not go backward. Restoring it from an old copy is not a
supported recovery: if the published index was rolled back or removed as
well, nothing in this tooling can tell that ledger from a current one, and it
would sign an index that forgets later withdrawals. A lost, damaged or
doubtful ledger stops the channel until a recovery is separately designed and
approved. The workflow has no operation that creates, resets or restores a
ledger.

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
4. **Promote.** After the mirror holds the release, dispatch
   `operation: promote` with the same rehearsal run id. Trusted prepare checks
   the same exact context and availability. The existing protected signer and
   writer approvals then authorize allocation, signing and publication of the
   index. Owner authorization is the promotion authority, not a rehearsal
   receipt.

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

Each run of the channel workflow is one of these, and every one needs a manual
dispatch on `master` and this run's approval.

| Operation | What it does |
| --- | --- |
| `release` | Publishes trusted-checked candidate assets after owner approval. Does not touch the index. |
| `promote` | Lists an owner-approved, published, mirrored release in the index. Allocates the next sequence. |
| `revoke` | Withdraws a release, naming the listed release affected machines move to and a public, credential-free reason. Allocates the next sequence. |
| `resume` | Publishes again, byte for byte, the index the ledger last allocated. No new sequence and no new signature, however long ago it was allocated. |

Nothing else allocates a sequence, and nothing runs on a schedule. An index
does not expire, so there is no renewal to perform or to miss, and no
operation re-signs an unchanged list. The signer host only has to be up for a
run that was approved; between runs it can be off, as long as its ledger
survives.

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

**When a run fails part way.** The signer commits the exact payload to the
ledger before it asks avault to sign, and commits the envelope before it
leaves the job. So a sequence is only ever signed over one payload, and a
failure leaves one of two states:

- Nothing was allocated. Dispatch the operation again.
- A sequence was allocated, and its envelope was or was not signed or
  published. Dispatch `resume`: it signs that same payload if it was never
  signed, and publishes the identical envelope. Publishing an envelope the
  bucket already holds writes nothing.

Another operation may also be dispatched instead of `resume`. It is planned
from the ledger, which already contains the unpublished change, so that
change is carried into the new index rather than lost. Sequences may skip.

### Availability of listed releases

`Guard Model Hub engine release` keeps available the engine Avibe packages and
every release the index lists, withdrawn ones and rollback targets included.
On its schedule it verifies each release's published assets against the
manifest, keeps a verified backup, and restores assets a release lost from
the newest backup that still verifies. It uploads only asset names that are
missing and never overwrites published bytes. It reads the index through the
keys Avibe ships and holds no channel key and no bucket credential, so it can
restore pinned bytes but cannot promote a release or undo a withdrawal.

### Authority

The channel workflow separates authority so that no job holds two:

- `authorize` holds no secret. It requires the reviewed workflow, dispatched on
  `master`, and reads both GitHub Environments from the API: each must exist,
  require review by a named user, and admit only `master`.
- `sign` runs on the signer host. The channel key stays inside avault there,
  beside the ledger. The job holds no bucket credential and runs only this
  checkout's scripts.
- `publish` holds the bucket credential and no key. It writes only over the
  object it read (`If-Match`), or only where none exists (`If-None-Match`). It
  refuses to replace a stored index signed by the channel key unless the new
  one strictly follows it, by sequence and by content.

`release`, `sign` and `publish` each prove, before their first privileged step,
that this run was approved for their Environment by one of its required
reviewers. A job that names an Environment nobody configured would otherwise
run unprotected.

Those checks are not an execution boundary for the signer host. A self-hosted
runner runs whatever its runner group admits, so `sign` names the runner
group, and that group must admit only this workflow at `refs/heads/master`.

A change counts as published only after the bucket returns the exact envelope
and the public URL serves those same bytes. The object is cached for 60
seconds, so the workflow waits for the public URL instead of purging it.

### Commissioning (owner-held, not done)

Commissioning does not turn `untrusted_rehearsal` into an authority. It remains
an owner-held action that requires the signer, ledger, protected environments and
custody boundaries below; this follow-up neither commissions nor verifies them.

Nothing below exists yet, and this repository creates none of it:

- **A channel key.** A dedicated secp256k1 key that is not a wallet key,
  generated and held inside avault on the signer. avault 0.1.10 cannot generate
  a secp256k1 key; that needs the avault release that adds
  `keypair generate --curve secp256k1` (avault#47, not yet released). Clients
  are unaffected: they only verify, which avault 0.1.10 does.
- **The public root.** A reviewed change adds the key id, the compressed public
  key and a sequence floor to `engine_channel_roots.json`. It must not come
  before the protections below are in place.
- **The signer host and its runner group.** A self-hosted runner in the group
  `model-hub-engine-channel-signer`, with avault, its store, the sealed key and
  a durable directory for the ledger. The group must admit only the channel
  workflow at `refs/heads/master`. Whether this organization's plan can
  enforce that restriction is **not verified**. Without it, any workflow run
  in the repository could reach the signer host, and the signer must not be
  commissioned. The host is needed only while an approved run signs. It does
  not have to stay up between runs; the ledger directory has to outlast them.
- **The ledger.** Created once, by hand on the signer host, with
  `scripts/model_hub_engine_channel.py initialize-ledger`, for a channel whose
  key has never signed an index. An empty directory is not proof of that; the
  owner asserts it. It is never run again. The ledger holds no secret and
  should be backed up, but a backup is a record, not something to restore and
  continue from.
- **Two GitHub Environments**, `model-hub-engine-channel-signer` and
  `model-hub-engine-channel-writer`, each with required reviewers and a branch
  policy of exactly `master`, and the repository variables and Environment
  secrets the workflow names. The writer's bucket credential is separate from
  the release mirror's.

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
