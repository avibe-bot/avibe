# Memory Runtime Release Safety Contract

> Status: active release-safety contract
>
> The abandoned Wave 3c Gate 5 transition, package rollback, quarantine,
> recovery-bootstrap, and successor sequencing designs are retired.

## Scope

This document retains the release behavior that current workflows enforce for
the independent `avibe-memory` distribution and Memory Runtime assets. It
covers release identity, immutable runtime bytes, publication ordering, public
availability, exact hash and archive validation, published-manifest
compatibility, and scheduled backup/recovery.

It does not define an upgrade rollback protocol, a package-transition verifier,
or a future release architecture. Historical scenario IDs
`MEMORY-INDEP-020`, `MEMORY-INDEP-022`, and `MEMORY-INDEP-023` remain retired and
must not be marked covered or reused for a different capability.

## Release Identity And Immutable Assets

- An official `v*` annotated tag is the release-state input. The workflow uses
  that tag and its source commit as one release identity.
- Every Memory Runtime manifest has `release_state: published`, names the same
  release tag, and points each archive URL at that tag's GitHub Release.
- Runtime archives and manifests are immutable per tag. A rerun may reuse a
  byte-identical asset, but a same-name mismatch fails and requires a new
  version. It must never overwrite the hosted runtime bytes.
- A GitHub-only `gh-v*` prerelease carries the `avibe-memory` and `avibe-os`
  wheel/sdist pairs, runtime archives, and manifests under the same identity.
  It does not trigger PyPI.

## Publication Order

The existing workflows keep one asset-complete GitHub Draft and one official
finalizer:

1. Derive one package version from the tagged source and use explicit VCS
   pretend-version variables for both `avibe-memory` and `avibe-os`.
2. Build both wheel/sdist pairs plus the Show and Memory Runtime archives and
   manifests from that source.
3. Verify the distributions have the same version, independent contents and
   metadata, and pass the core-only/core-plus-Memory installation matrix.
4. Verify the locally staged Memory Runtime manifest and exact asset set before
   any release publication.
5. Create or reuse the Draft for that tag. Check every existing runtime asset
   for byte identity before mutating any release asset, then upload both
   distribution pairs and the runtime assets.
6. `Release (AI Notes)` may update the Draft notes and assets, but it never
   publishes an official release.
7. `Publish to PyPI` verifies the exact notes run and the asset-complete Draft,
   then publishes the GitHub Release before either distribution reaches PyPI.
8. Publish only `avibe-memory` through its trusted-publishing environment with
   skip-existing semantics. Retry a direct PyPI wheel download without
   dependencies or cache and byte-compare it with the staged wheel. An existing
   non-identical wheel fails closed.
9. Publish only `avibe-os` through its existing trusted-publishing environment
   after the public `avibe-memory` wheel verification succeeds.

A release failure resumes through the same tag and Draft only when all existing
runtime bytes are identical. It does not mint a replacement identity, publish
around a failed integrity check, or invoke automatic package rollback or
lifecycle recovery.

## Guard CLI Contract

Current workflows use exactly three
`scripts/memory_runtime_release_guard.py` subcommands:

- `check-policy` validates that a manifest describes a supported published
  EverOS provenance and has the required platform, same-release URL, size,
  digest, binary path, and optional sync-bootstrap fields.
- `verify --asset-dir <dir>` requires the exact asset-name set, exact manifest
  bytes, declared archive size and SHA-256, the declared runtime binary hash,
  and any declared sync-bootstrap member hashes and marker bytes.
- `fetch --output-dir <dir>` downloads the manifest and archives from the
  manifest's release tag with declared-size bounds, verifies the complete set,
  and replaces the destination only after verification succeeds.

Policy exclusions, byte failures, and internal guard failures retain distinct
exit codes and JSON failure kinds. These paths are stdlib-only and do not depend
on wheel metadata or a static package-transition policy.

## Published Compatibility And Migration

Already-published Memory Runtime manifests remain verifiable through the
explicit `PUBLISHED_RUNTIME_PROVENANCE` ledger. Moving the current EverOS pin
does not remove older supported entries. A manifest with unsupported provenance
is excluded visibly by `check-policy`; it is not treated as a byte-recovery
failure.

For split releases, the `avibe-memory` wheel is the authoritative manifest
owner. A published release may fall back to its legacy `avibe-os` wheel only
when no `avibe-memory` wheel exists. A present but missing or invalid Memory
manifest is excluded visibly and never masked by the legacy fallback. Legacy
core wheels that predate the Memory Runtime manifest are skipped by the
scheduled inventory. Any candidate manifest must self-pin the selected release
tag and pass current policy before it enters guarded backup and recovery
coverage. Changes to a shipped manifest shape require compatible loading or a
deliberate visible exclusion, never startup or workflow failure by accident.

## Availability, Backup, And Recovery

The scheduled `Memory Runtime Release Guard` preserves the managed-runtime
availability contract:

1. Enumerate published releases with either distribution wheel. Prefer
   `avibe-memory`; select `avibe-os` only for a release with no Memory wheel.
2. Extract the manifest from that selected wheel, record its owner, exact wheel
   pattern, and manifest hash, then run `check-policy` and report guarded and
   excluded releases separately.
3. Re-download the exact recorded owner during verification instead of
   rediscovering package ownership.
4. Run `fetch` for every guarded manifest. A successful fetch is the public
   availability and integrity check.
5. Keep a verified backup artifact keyed by the exact manifest SHA-256.
6. On a byte failure, locate the latest non-expired matching backup, run
   `verify` on it, and upload only asset names that are missing from the release.
   Existing names are never clobbered; a same-name integrity failure stops.
7. Run `fetch` again after recovery so restored public bytes must pass the same
   release-tag, manifest, archive, and hash checks.

The backup lookup follows the artifact identity rather than a fixed recent-run
window, and the retained backup remains tied to one exact manifest hash.

## Operator Safety

- Do not pre-create or manually edit an official `v*` GitHub Release; the
  annotated tag and workflows own release state.
- Before the first `avibe-memory` publication, configure its PyPI
  pending/trusted publisher for repository `avibe-bot/avibe`, workflow
  `publish.yml`, and GitHub environment `pypi-avibe-memory`. This external
  configuration is a required operator action and is not performed by CI.
- Do not overwrite a mismatched runtime asset, weaken a digest, bypass the
  public re-fetch, or continue to PyPI before the GitHub Release is published.
- Do not classify policy exclusions as recoverable missing bytes.
- Recovery may restore missing immutable assets only from a verified backup for
  the same manifest hash and release identity.
- Verification and maintenance must not restart local Avibe, mutate Incus, or
  perform release actions outside the release workflows.
