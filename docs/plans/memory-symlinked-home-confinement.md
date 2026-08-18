# Memory symlinked-home confinement

Issue: [#1520](https://github.com/avibe-bot/avibe/issues/1520)

## Background

Memory currently derives several mutable paths from the logical Avibe home, but
its attachment store, SQLite store, provider-root guard, and process supervisor
do not share one trust-anchor decision. A supported home reached through a
symlink can therefore fail an absolute no-follow walk before the walk reaches
anything Avibe owns.

## Goal

Resolve the operator-controlled effective home once to a physical trust anchor,
derive Memory-owned paths lexically below it, and preserve no-follow rejection
for every Avibe-owned descendant.

## First implementation slice

- add one confined-root value in `core/memory/confined_filesystem.py` that maps
  logical and already-physical child spellings to the same physical root without
  resolving child components;
- make `MemoryRuntime`, `MemoryStore`, `AttachmentPinStore`, `ProviderRoot`,
  `EverOSProcess`, and `EverOSRebuildProcess` consume that physical identity;
- route attachment storage layout and Memory-store directory preparation through
  the existing confined-filesystem interface while retaining domain error types;
- add contract tests for a symlinked parent, the legacy final-home symlink shape,
  one physical provider/lock identity, and rejection of symlinks below the home.

## Non-goals

- no changes to attachment limits, modality, hashing, bundle lifetime, SQLite
  transaction semantics, provider sentinel semantics, or child lifecycle;
- no change to explicitly configured provider roots or socket paths outside the
  effective home;
- the remaining caller audit (`operation_lock`, call-log storage, artifacts,
  clear intent, and factory reset) stays tracked by #1520 and is not declared
  complete by this first slice.
