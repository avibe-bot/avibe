# Memory as a GitHub Release companion

## Decision and scope

The owner approved GitHub-only distribution of the optional Memory package on
2026-09-16. Core remains on PyPI. This replaces the separate Memory PyPI
publisher, not Memory itself. The root owns implementation, gated integration,
and gated publication. The owner explicitly chose replacement of the partially
published GitHub v3.1.0 on 2026-09-16, not a new version. That one-time operation
must preserve prior publication evidence and reconfirm PyPI absence before
withdrawal. Generic workflows still reject changed existing assets; they do
not implement withdrawal or an overwrite bypass. This change does not install
or restart local Avibe.

## Invariants

- Both workflows still build and validate both wheel/sdist pairs and all runtime
  assets. The companion requires exactly the same normalized core version.
- Core metadata has no Memory index dependency. The old `memory` extra remains
  an empty compatibility spelling; enabling Memory uses the existing package
  reconciler, not Python extras.
- A PyPI core install selects the companion wheel at official `v<version>`.
  Official tags must use that canonical normalized spelling; the shared
  publication owner rejects aliases before any release mutation, even when
  a workflow dispatch builds older source. Existing official tags are canonical.
  An exact core wheel from this repository's GitHub Release selects its same
  tag, including `gh-v` previews. Forward upgrades derive this from their
  target artifact, never the currently installed preview's origin.
- Every install/preflight command uses the explicit companion source. Missing
  assets fail before activating a new installation; there is no PyPI fallback
  for Memory, new updater, rollback protocol, or runtime preparation.
- Official release order is complete published GitHub assets, anonymous public
  byte verification of the companion wheel and sdist, then core PyPI.
  Anonymous download propagation (including transient 404) has bounded
  retries. Successfully downloaded but different bytes fail immediately.
- Both upload workflows compare all existing package/runtime bytes before any
  asset upload, retain identical assets, and upload only missing assets.
  Artifact filenames use normalized package versions; release URLs retain the
  original supported tag spelling.
- Existing source/editable workflows remain local and preserve the new Memory
  subprocess-lifecycle fix and dependency floor.

## Migration boundary

Older released updaters request Memory from PyPI before the new core can run.
They cannot be transparently repaired by new code. Users of that path must
update core through the official installer/core-only installation entry first.
On the next normal startup, enabled Memory uses the existing reconciler to
obtain the matching GitHub companion. Disabled Memory remains core-only.
Do not promise every old client's in-place update works. The released 3.0.13
core-only first hop is exercised separately; the next startup must obtain
Memory through its GitHub URL, not the package index.

## Validation

Exercise source selection, false origins, exact and forward pip/uv plans,
preflight failure, core-only installs, and existing reconciliation behavior.
Build genuine wheel/sdist pairs at the proposed release version and run the
artifact-bound matrix with no skipped cases. Real isolated resolver tests must
place Memory outside the index and preserve exact peer selection in the
presence of newer decoys. Keep Windows/runtime/build/release CI gates.
