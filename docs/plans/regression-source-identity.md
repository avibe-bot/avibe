# Regression Source Identity

## Background

The Incus regression runner syncs the active source tree on every update, but
only refreshes the editable Python installation when dependency inputs change.
`vibe.__version__` can therefore describe an older editable-install build while
the service is executing newer backend source and UI assets. Using that package
version as the deployed build identity also produces false PyPI update prompts.

## Goal

- Identify the exact source revision served by local Incus regression targets.
- Keep the package version and its semantic update behavior unchanged for
  packaged installs.
- Avoid reinstalling unchanged Python dependencies just to refresh display
  metadata.

## Design

The regression runner already rewrites `/var/lib/avibe-regression/metadata.json`
after every source sync with the deployed commit and dirty state. The systemd
service will declare that file through `VIBE_BUILD_METADATA_PATH`. A small
shared build-identity reader will expose it as a source build without treating
the Git revision as a package version.

`/api/version` remains backward compatible: `current`, `latest`, `has_update`,
and `error` keep their existing meanings for packaged installs. It gains a
`build` object. Source builds report their revision there and skip product
package update checks; the UI shows the source revision as the primary badge
identity and labels the stale install-time value as package metadata.

No scenario catalog entry currently covers regression deployment identity.
Focused runner, API, and update-checker tests cover this boundary.

## Acceptance

- Two metadata refreshes with an unchanged dependency fingerprint produce two
  different source identities without rebuilding package metadata.
- Source deployments do not advertise a PyPI update from stale package data.
- Packaged deployments retain the existing semantic version comparison.
- The local worktree Incus environment reports the branch head through
  `/api/version` after repeated source syncs.
