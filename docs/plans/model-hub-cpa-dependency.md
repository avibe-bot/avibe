# Model Hub CPA Dependency Lifecycle

## Context

Model Hub runs through a managed CLIProxyAPI (CPA) binary. Avibe already pins
that binary in a packaged, checksummed manifest, but installation was previously
entered only through Model Hub's feature-specific runtime flow. As a result, an
Avibe upgrade could leave an older CPA selected on disk, and Settings did not
show the dependency or its target version.

## Decision

CPA is a required Avibe dependency, independent of whether Model Hub is enabled
or has ever been used. Each Avibe release owns one reviewed CPA target. User
machines converge to that target; they never resolve an upstream `latest` tag at
runtime.

Installation and execution remain separate lifecycle decisions:

- `vibe runtime prepare` and the background startup dependency reconcile install
  or upgrade CPA to the manifest packaged with the current Avibe release.
- The Dependencies settings page reports both the installed version and the
  packaged target and offers install, update, or repair when convergence is
  possible.
- Installing or upgrading CPA does not enable Model Hub or start a stopped CPA
  process. When the controller already owns a running process, replacement goes
  through that owner so the process restarts on the verified target. A UI-side
  reconcile probes the starting controller's health endpoint rather than
  treating a Unix-socket pathname, including a stale one, as readiness. The
  install-job poll budget outlasts the complete controller-readiness and CPA RPC
  bounds.
- Every Model Hub demand path verifies the packaged target before starting CPA,
  so a stale pointer cannot be started after a failed or skipped background
  reconcile.
- CPA remains visible on hosts for which Avibe publishes no compatible asset,
  but that `unsupported` capability state is derived from Avibe's fixed host
  matrix and the selected manifest's asset set, not mutable install or pointer
  state. It is nonfatal and offers no repair that cannot succeed.

## Failure Policy

CPA installation is atomic. A failed download, verification, or candidate
preparation leaves the prior verified install and current pointer intact. It is
reported in Dependencies and Doctor and can be retried manually or by the next
startup reconcile. Every CPA failure token exposed by Dependencies has English
and Chinese copy enforced by a backend-to-locale contract test. CPA failure does
not roll back or fail an otherwise successful Avibe upgrade.

A synchronous dependency ensure settles only the exact durable install state it
observed on entry. Success clears an unchanged prior failure or orphaned install
claim, while a claim replaced by a newer owner remains untouched.

Successful replacement retains one prior managed install for rollback and
cleans older generations through the existing managed-runtime cleanup policy.

## Release Maintenance

Updating CPA for an Avibe release requires changing the packaged manifest in the
same change as its frozen contract test. The release tag and source commit are
fixed, and every supported archive records its published byte size, archive
SHA-256, and extracted binary SHA-256. The verified upstream bytes are published
  under an Avibe-owned release tag before the manifest is pinned. For patched
  dependencies, the assets are built from the reviewed source and patch before
  publication. A scheduled guard verifies that release and preserves a
  manifest-keyed backup that can restore missing assets without overwriting a
  mismatched asset. For this change,
the reviewed target is Avibe-owned CLIProxyAPI `v8.0.30` at source commit
`5be2cdab0869cbaa3d401891f740aebf7e15aa99`, built with Go 1.26.10 for
darwin-arm64, darwin-x64, linux-amd64, linux-arm64, windows-amd64 and
windows-arm64. The release assets are built from that exact upstream source
with the patches in `patches/model-hub-cpa-v8.0.30/`, applied in the order the
manifest's `build.patches` records; the packaged manifest records the resulting
archive and binary digests. Rolling back means reverting the whole adoption
change, which restores the previous patch directory and manifest together.

### Hosted passthrough

Hosted tools rely on the engine passing them through unchanged, and they are
assumed rather than probed, so a passthrough regression in an engine release
would reach users unnoticed. `lint` guards it as `model-hub-hosted-wire`, which
reports through the required `unit-tests` check, on every pull request that
replaces the engine (changes the manifest's `asset_release_tag`):

- `scripts/model_hub_hosted_wire_gate.py` downloads both engines' assets for the
  runner's platform from their published releases and verifies them against
  each manifest. It then runs the cases in
  `tests/e2e/test_model_hub_engine_hosted_passthrough.py` against both and
  compares each case's upstream request path, query, header set and bytes, and
  its downstream status and bytes. Only per-run values are masked: the engine's
  `prompt_cache_key` and `session-id`, and the port in the capturing upstream's
  `Host`. The new engine must pass every case, where a case marked
  `xfail(strict=True)` passes by failing as its marker says. The base engine
  only has to record each case, so a case it fails is compared as recorded.
  An engine that fixes a known defect turns its strict expected failure into a
  failure, so that change flips the marker and carries the label below.
- A byte-identical passthrough passes. A difference fails and names the cases
  and parts that differ, unless the pull request is labeled
  `engine-hosted-wire-change`. An intended change, a rollback included, needs
  that label and a note in the pull request body saying what changed and why.
  The gate reads the labels when it runs, adding or removing one re-runs
  `lint`, and labels that cannot be read count as none.

The gate judges pull requests, where the label lives; a push to master does not
repeat it.

Before dispatching an engine build, run the gate on the local candidate build
and include its output in the dispatch request; engine sign-off requires it.
Without a pull request no label applies, so the output shows any difference as
a failure for review:

```sh
python scripts/model_hub_hosted_wire_gate.py --base origin/master --head-assets <build output dir>
```

The pull request's gate stays the enforcement of record.

## Acceptance Criteria

- A user who never enables Model Hub receives the CPA target during normal Avibe
  install, upgrade, or startup reconciliation.
- A user upgrading Avibe from an older CPA pin converges to the new pin without
  following future upstream releases.
- A stopped Model Hub remains stopped after dependency convergence; a running
  one restarts only after the replacement is verified and activated.
- Dependencies and Doctor expose missing, stale, unsupported, and failed states
  without claiming an unknown target is current; unsupported hosts receive a
  warning rather than a failing required-dependency verdict.
- A failed CPA update preserves the previous runnable version and does not block
  the Avibe upgrade.
