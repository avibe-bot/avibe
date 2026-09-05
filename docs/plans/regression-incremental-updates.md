# Incremental Regression Updates

## Goal

Reduce repeated writes and contention on the local Incus VM disk without moving
storage, changing resource limits, resetting product state, or restarting the
host Avibe service.

## Design

- Use the existing base image's rsync through the Incus exec transport. Sync
  changed source files and delete stale source, preserving excluded dependency
  and runtime trees. Preserve executable modes and symlinks, but create files
  as the service user rather than importing host ownership. Use rsync's normal
  size/mtime quick check, not a full destination-content checksum pass.
- Reuse the Show Runtime archive only when its receipt matches the resolved
  upstream commit, target Node/platform and npm versions, build recipe, and
  archive checksum. Build the exact resolved commit; atomically publish the
  archive and publish its receipt last. Keep one artifact, not an unbounded
  cache. Always run the existing runtime preparation/validation path.
- Reuse the runner's file-lock mechanism for a shared heavy-I/O slot across
  environments managed by the same primary checkout and daemon. Acquire it
  before instance creation or stopping the target service, and hold it through
  update/recovery. Base-image builds use the same slot. Other services remain
  online; updates using older runners do not participate until updated.

## Invariants and Verification

- Repeating an unchanged sync preserves file inodes and timestamps. A changed
  source tree converges including removals, modes, links, and directory/file
  transitions; excluded trees are neither copied nor descended into.
- Reuse requires both matching build inputs and matching artifact bytes.
  Failed builds do not mark an artifact as current. Upstream changes between
  resolution and fetch cannot silently select a different revision.
- Different environments sharing the I/O slot cannot run heavy update phases
  concurrently. Queued updates keep their existing service running. Exceptions
  release locks after recovery; dry runs do not acquire locks.
- Run focused unit/real-filesystem tests, then local Incus repeated updates and
  health checks with existing state and pairing preserved. Verify source sync
  statistics and Show Runtime reuse output; do not invent performance claims
  from tests alone.

## Status

- [x] Inspect update lifecycle, existing exclusions, fingerprints, and locks.
- [x] Implement the three optimizations and focused coverage (378 focused tests;
  real macOS-to-Incus fixture: unchanged sync transfers zero file bytes, a single
  edit transfers one file, Linux unchanged inode/mtime/ctime and ownership stay
  intact, and obsolete source is removed).
- [ ] Pass Codex review and expected CI through the managed delivery loop.
- [ ] Merge and verify local Incus update/reuse and Models health.
