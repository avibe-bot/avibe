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
  reconcile waits for a starting controller's internal endpoint rather than
  treating an unbound socket as permission to install concurrently.
- Every Model Hub demand path verifies the packaged target before starting CPA,
  so a stale pointer cannot be started after a failed or skipped background
  reconcile.
- CPA remains visible on hosts for which Avibe publishes no compatible asset,
  but that `unsupported` capability state is nonfatal and offers no repair that
  cannot succeed.

## Failure Policy

CPA installation is atomic. A failed download, verification, or candidate
preparation leaves the prior verified install and current pointer intact. It is
reported in Dependencies and Doctor and can be retried manually or by the next
startup reconcile. CPA failure does not roll back or fail an otherwise
successful Avibe upgrade.

Successful replacement retains one prior managed install for rollback and
cleans older generations through the existing managed-runtime cleanup policy.

## Release Maintenance

Updating CPA for an Avibe release requires changing the packaged manifest in the
same change as its frozen contract test. The release tag and source commit are
fixed, and every supported archive records its published byte size, archive
SHA-256, and extracted binary SHA-256. The verified upstream bytes are published
under an Avibe-owned release tag before the manifest is pinned. A scheduled
guard verifies that release and preserves a manifest-keyed backup that can
restore missing assets without overwriting a mismatched asset. For this change,
the reviewed target is CLIProxyAPI `v7.2.149` at source commit
`2a6b87aca083a5bf498ac1f68a1b636c500d7aaa`.

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
