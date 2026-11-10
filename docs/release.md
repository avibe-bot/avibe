# Avibe Release Workflow

This document contains the release-specific details that are too operational
for `AGENTS.md`. The workflows are the source of truth for the final commands.

## Select the release commit

Fetch `origin/master` and record the candidate's exact SHA for all release-only
validation and tag creation; do not switch it silently while checks run. New PRs
may continue to merge: there is no routine release-time merge freeze.

Require a successful **full master backstop at the exact candidate SHA** for
every release, stable included. `lint.yml` runs that full pass after master
pushes, including iOS, desktop, browser, installer, web-fetch and Windows
checks. A running pass finishes; only the newest pending master pass waits.
`release_ai.yml`, `publish.yml`, and `desktop-release.yml` enforce the full
master backstop before their respective build, signing, or publication paths.
See [CI scope and routing](plans/ci-affected-routing.md). The PR-only iOS
NavigationPolicyTests quarantine (#3418) does not relax this release gate:
iOS must pass in the exact full master run, even while its PR check is advisory.

A green PR head, another SHA's full run, or a missing/coalesced run is not
proof. Select a full-green master SHA; wait for its current full pass, or rerun
its existing master backstop. Manual full validation is available on master.
Historical runs with no explicit full-backstop certificate are not assumed
equivalent.

Before pushing a tag, fetch master again. If the release is required to include
newly landed work, select the new tip and repeat candidate-specific gates; otherwise
retain the recorded, validated candidate. Require the owner's release authorization
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

### One signed index

`https://dl.avibe.bot/index/model-hub-engine.json` is the only record of which
engine releases may be adopted and which were withdrawn. It is a signed
envelope around an exact payload; each release entry carries the minimum Avibe
version and the exact bytes of that release's manifest. Clients trust it only
through a key shipped inside Avibe, never because of where it was downloaded.

`Publish Model Hub engine channel` is its only writer. Each run makes one
change to the index that is published now, and copies every release and
withdrawal already in it byte for byte. Nothing regenerates the index from the
list of GitHub releases, and `Mirror releases to dl.avibe.bot` refuses to write
or delete that object.

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
   then signs and publishes an index that lists the release.

A receipt is not taken on trust. Before publishing or promoting, the workflow
reads the qualification run from GitHub: the workflow file, a commit on
`master`, the run attempt, and a successful conclusion for every job. It then
requires the receipt to name the reviewed record, the qualification harness as
it is on `master` now, the Avibe wheels as GitHub serves them now, and every
baseline engine the index lists now. A newer stable Avibe, a changed harness,
or a newly listed engine means qualifying again.

| Evidence | What it shows | Producer today |
| --- | --- | --- |
| `build` | The pinned source rebuilds to the reviewed manifest and six assets. | Yes |
| `platform_smoke` | Each archive starts on its own platform and passes Avibe's health check. | Yes; the producer runs in PR CI against the packaged engine |
| `hosted_wire` | The hosted passthrough compared with every baseline engine. A difference needs a reviewed reason in the record. | Yes; not yet run |
| `released_avibe_e2e` | The full hermetic Model Hub E2E, driven through the released wheel of the minimum Avibe and of the current stable Avibe. | Yes; not yet run, and cannot be before stable 3.2.6 exists |
| `backward_state` | State written by the candidate, read back by every baseline engine. | No. Arrives with the engine lifecycle work. |
| `lifecycle` | Activation, rollback and recovery of the candidate. | No. Arrives with the engine lifecycle work. |

The last two are required like the others, so no receipt can be produced, and
no engine promoted, until their producing jobs exist. The released-Avibe job
installs the published wheel into its own environment and runs that release's
own tests from a directory that holds no product package; the run records where
it imported Avibe from, and a run that imported a checkout is refused. No
engine may declare a minimum below 3.2.6, the first Avibe that reads the
channel, so the first promotion also waits for the stable 3.2.6 artifacts.

### Renewal

The index is valid for seven days. `operation: renew` re-signs the index
published now with the next sequence and new dates, and changes nothing else.
No schedule runs it yet; enabling a daily renewal is part of commissioning. An
index that expires stops new adoption on clients. It does not stop a working
engine, and withdrawals a client already recorded stay in force.

### Withdrawal

`operation: revoke` withdraws a release. It needs the release tag, the listed
release that affected machines move to, and a public, credential-free reason.
The withdrawal is added to the index permanently: later indexes must carry it
unchanged, clients refuse an index that drops it, and the writer refuses to
publish one. The release's assets and its index entry stay. The fallback must
be a listed release that was not withdrawn and that does not need a newer
Avibe than the withdrawn one. An engine that only a packaged Avibe bundles can
be withdrawn too.

Clients learn of a withdrawal at their next check. A machine that is offline
learns of it when it reconnects.

### Authority

The channel workflow separates authority so that no job holds two:

- `authorize` holds no secret. It requires the reviewed workflow, dispatched on
  `master`, and reads both GitHub Environments from the API: each must exist,
  require review by a named user, and admit only `master`. A job that names an
  Environment nobody configured would otherwise run unprotected.
- `sign` runs on the dedicated signer runner. The channel key stays inside
  avault there; the job holds no bucket credential, runs only this checkout's
  scripts, and plans the payload itself from the index published now.
- `publish` holds the bucket credential and no key. It writes only over the
  object it read (`If-Match`), or only where none exists (`If-None-Match`), so
  a stale or racing writer fails instead of replacing a newer index.

`release`, `sign` and `publish` each prove, before their first privileged step,
that this run was approved for their Environment by one of its required
reviewers. Renewal and withdrawal go through the same approval as promotion.

A change counts as published only after the bucket returns the exact envelope
and the public URL serves those same bytes. The object is cached for 60
seconds, so the workflow waits for the public URL instead of purging it. Every
published envelope is archived as a workflow artifact. If the object is lost
or damaged, `recover_from_archive` continues from the newest archive that
verifies, withdrawals included. A first publication is refused once any
archive exists.

### Commissioning (owner-held, not done)

Nothing below exists yet, and this repository change creates none of it:

- **A channel key.** A dedicated secp256k1 key that is not a wallet key,
  generated and held inside avault on the signer. avault 0.1.10 cannot generate
  a secp256k1 key; that needs the avault release that adds
  `keypair generate --curve secp256k1` (avault#47, not yet released). Clients
  are unaffected: they only verify, which avault 0.1.10 does.
- **The public root.** A reviewed change adds the key id, the compressed public
  key and a sequence floor to `engine_channel_roots.json`. Adding it must not
  come before the protections below are in place.
- **The signer runner.** A self-hosted runner labelled
  `model-hub-engine-channel-signer`, with avault, its store and the sealed key.
  Any workflow run in the repository can ask for a self-hosted runner, so its
  runner group must admit only the channel workflow on `master`.
- **Two GitHub Environments**, `model-hub-engine-channel-signer` and
  `model-hub-engine-channel-writer`, each with required reviewers and a branch
  policy of exactly `master`, and the repository variables and Environment
  secrets the workflow names. The writer's bucket credential is separate from
  the release mirror's.
- **A renewal schedule**, added by a reviewed change once the above works.
  Renewal needs the same approval as any other change, so an unattended
  schedule and per-run approval cannot both hold. How renewals are approved is
  decided then.

Before the first promotion, the engine release guard must also verify, back
up and recover every release the index lists, not only the packaged engine.

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
`https://dl.avibe.bot/index/releases.json`. It follows `Publish to PyPI`,
`Release (AI Notes)`, and `Desktop Release (Stage 2)`, so either stage can
reconcile the same release without overwriting immutable bytes. It also keeps
version-less links to the newest stable desktop installers, such as
`https://dl.avibe.bot/latest/Avibe_aarch64-apple-darwin.dmg`. The contract and retention rules are in
`docs/plans/release-download-mirror.md`.
