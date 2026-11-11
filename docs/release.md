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

GitHub-only pre-releases use the `gh-vX.Y.ZrcN` format, such as
`gh-v2.2.8rc2`, so they remain distinct from PyPI-triggering `v*` tags. They
must include an installable wheel containing `ui/dist` and
`vibe/show_runtime/*.tgz`, plus the source distribution.

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
2. **Qualify.** Dispatch `Qualify Model Hub engine candidate` on `master` with
   the record's tag. It holds no secret. It rebuilds the pinned source and
   requires the reviewed digests, then gathers evidence about those exact
   bytes. The run produces a receipt only when every class below is present.
3. **Publish the assets.** Dispatch the channel workflow with
   `operation: release` and the qualification run id. It re-checks the receipt
   and publishes the qualified bytes as an immutable, non-latest release. It
   never rebuilds them. Publication alone makes no engine adoptable.
4. **Promote.** After the mirror holds the release, dispatch
   `operation: promote` with the same run id. It re-checks the receipt, requires
   every asset from both the release and the mirror byte for byte, and only
   then allocates, signs and publishes an index that lists the release.

A receipt is not taken on trust. Before publishing or promoting, the workflow
reads the qualification run from GitHub: the workflow file, a commit on
`master`, the run attempt, and a successful conclusion for every job. It then
requires the receipt to name the reviewed record, the qualification harness as
it is on `master` now, the Avibe wheels as GitHub serves them now, and every
baseline engine the published index lists now. Those baselines are handed to
the signer, which refuses to promote unless they cover every release its own
ledger lists. A newer stable Avibe, a changed harness, or a newly listed
engine means qualifying again.

| Evidence | What it shows | Producer today |
| --- | --- | --- |
| `build` | The pinned source rebuilds to the reviewed manifest and six assets. | Yes |
| `platform_smoke` | Each archive starts on its own platform and passes Avibe's health check. | Yes; the producer runs in PR CI against the packaged engine |
| `hosted_wire` | The hosted passthrough compared with every baseline engine. A difference needs a reviewed reason in the record. | Yes; not yet run |
| `released_avibe_e2e` | The full hermetic Model Hub E2E, driven through the released wheel of the minimum Avibe and of the current stable Avibe. | Yes; rehearsed against a release candidate, not run for qualification |
| `backward_state` | State written by the candidate, read back by every baseline engine. | No. Arrives with the engine lifecycle work. |
| `lifecycle` | Activation, rollback and recovery of the candidate. | No. Arrives with the engine lifecycle work. |

The last two are required like the others, so no receipt can be produced, and
no engine promoted, until their producing jobs exist. No engine may declare a
minimum below 3.2.6, the first Avibe that reads the channel, so the first
promotion also waits for the stable 3.2.6 artifacts.

Evidence counts only under the job that produces its class. The receipt job
reads each class from the artifact named for its job, refuses any document
that job does not produce and any artifact named for no job, and records the
job on what it accepts. A class with no producing job is accepted from
nowhere, whatever a job uploads. The finalizer applies the same rule, so a
receipt that carries all six classes is refused when it is made and when it
is checked, until both missing producers exist.

That rule is an allowlist by artifact name and document shape. It does not
authenticate which job uploaded an artifact.

**Containing the candidate is unfinished.** The smoke, hosted-wire and
released-Avibe jobs execute the candidate as the user that writes their
evidence, beside the run's artifact credential. A candidate that attacks its
runner can make such a job report anything about itself and could replace an
artifact another job uploaded. What stands against that today is the review
of the candidate's record and of the pinned source, which the `build` job
compiles without running it and must reproduce to the reviewed digests.

This is qualification engineering owed in this repository, not an
owner-held step, and there is no switch that waives it. The change that
gives the last evidence class a producer does not merge before the
containment is implemented, reviewed independently and exercised. The first
usable receipt, the first promotion and commissioning all wait for it. What
the containment must show is in `docs/plans/model-hub-engine-channel.md`.

The released-Avibe job installs the published stable wheel into its own
environment and runs **that release tag's own tests** from a directory that
holds no product package. The service starts from the wheel's own entry
script. Every Python process in that environment logs where it imported each
Avibe module from, and the run is refused if any module, in the test runner,
the service or a helper, came from anywhere else. A module is imported only
once its process's log accounts for it, and a run is also refused if any
process's log is marked as not holding every import.

`Rehearse the released-wheel Model Hub E2E` runs the same shared steps against
one pinned release candidate, so the harness is exercised before the stable
tag freezes its tests. It has two legs. One runs the candidate's own tagged
tests and only reports what fails, since a tag cannot be fixed. The other runs
the current tests against the same wheel and must pass. A rehearsal qualifies
nothing: it does not use the stable-only resolver, and its record is of a
kind no receipt reads.

### Operations

Each run of the channel workflow is one of these, and every one needs a manual
dispatch on `master` and this run's approval.

| Operation | What it does |
| --- | --- |
| `release` | Publishes a qualified candidate's assets. Does not touch the index. |
| `promote` | Lists a qualified, published, mirrored release in the index. Allocates the next sequence. |
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

Commissioning also waits for the containment of candidate execution
described under qualification above. That is engineering in this repository.

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
