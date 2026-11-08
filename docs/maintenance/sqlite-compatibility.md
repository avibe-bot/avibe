# SQLite compatibility epoch: bridge delivery

Implementation of section A in [the downtime plan](../plans/sqlite-upgrade-downtime.md).
This document describes the bridge, not activation of snapshot-free upgrades.

## Change contract

- Revision `20261108_0076` establishes integer `state_meta.compatibility_epoch = 1`.
  Its schema is otherwise unchanged. Existing non-empty databases still get a
  fresh verified snapshot on upgrade; fresh databases need no snapshot.
- `storage.migrations.preflight_database` is the common decision owner.
  `MigrationPreflight.compatible_open` means the installed binary may use the
  newer schema as-is, not run Alembic against it. A full unknown exact revision
  with a supported epoch must have all required tables/columns and hard
  `INDEXED BY` indexes. New required columns must not invalidate older omitted-
  column writes. Partial/symbolic/mixed stamps, unsupported/malformed epochs,
  inconsistent required schema, unrelated branches and ancestor targets refuse
  before backup, repair or import.
- `run_migrations` returns that decision. Bootstrap/import returns immediately
  on compatible-open, without touching lifecycle receipts, replaying data
  migrations, repairing indexes or adopting a prior backup attempt. Explicit
  stores and engine factories use the same preflight; the factory caches a
  successful admission per process/path/inode, matching the existing importer
  process-lifetime contract. File replacement is rechecked. Breaking upgrades
  require **all** controller/Web/CLI writers stopped; this is not a lease.
- A known forward upgrade may lack the new binary's columns until its migrations
  run. An epoch marker on a known graph must match the epoch declared by that
  revision's ancestry. A supported unknown revision is the only intentional
  relaxation of the exact-revision rule in the original preflight plan.
- Every bridge-and-later revision starts a real SQLite transaction before DDL.
  DDL/data, Alembic stamp and epoch commit together. The final head's required
  schema is validated before commit, without a full-file scan. Transaction
  escape, ATTACH/DETACH and setting pragmas from new migration bodies are denied;
  a migration that needs them must be redesigned. Historical migration behavior
  is unchanged. Old schema-repair helpers never run on an epoch-marked database.
- Within a supported epoch, select the older binary; do **not** Alembic-downgrade
  or restamp. Crossing epochs or returning to a pre-bridge binary requires an
  explicit verified restore with all writers stopped. **Restore discards writes
  since the snapshot.** Prefer forward repair. Already-released binaries cannot
  be retrofitted: users must not invoke their raw SQLite APIs to bypass startup.

## API for later representation changes

`storage.compatibility` owns the key, strict parser, supported-reader epoch set,
bridge revision and migration-only `advance_compatibility_epoch(connection, N)`.
A semantic change without DDL still needs a new breaking Alembic revision with
literal `MIGRATION_COMPATIBILITY = "breaking"` and `COMPATIBILITY_EPOCH = N`;
advance the marker inside that revision's transaction. The runtime's supported
reader set is separate from parsing a known old epoch for forward upgrade.

C-alt must first ship projection-aware readers, while retaining compatibility
traces. Do not suppress traces based only on the presence of epoch 1: the epoch
must identify a **released projection-aware reader floor**, approved with that
lane. The bridge by itself does not claim any projection capability.

## Release boundary (owner decision, 2026-11-08)

**Bridge release (R0)** means the first release of any kind, RC or stable, whose
binaries implement protocol v1. A prerelease establishes the bridge for each
DB it opens; the per-DB integer marker is the authority, not a version number.
The next release containing this implementation carries the bridge, and later
stable releases carry the same protocol. This PR cuts no release or version.

**First no-copy release (R1)** means a later release containing no-copy activation.
It may skip a snapshot only when the DB already has valid bridge evidence from
a different, earlier binary generation, so the rollback binary understands the
epoch. A marker first written during the current upgrade is insufficient. The
activation gates on marker and previous-generation evidence, never semver.
Rolling back to any pre-protocol binary still requires the bridge-time backup.
The bridge always retains conservative upgrade snapshots.

Remaining A work, after the bridge contract lands:

1. CI-derived operation allowlist with breaking as default, retaining the
   existing release-graph guard. No relabeling of historical migrations.
2. The immediately previous release's real reader/writer contract test. Before
   the bridge is released, that previous release does not implement this protocol
   and is **not** a supported binary rollback target.
3. Additive no-copy activation with valid existing bridge marker only, plus
   exact-attempt backup/expiry tests and synthetic-volume latency evidence.
   No receipt adoption or expiry-clock resets on the no-copy path.
4. Align public English/Chinese upgrade documentation for the bridge and its
   later activation. No public claim of a released snapshot-free path before then.

## Validation

Primary owner coverage: `tests/test_sqlite_compatibility_epoch.py`. It exercises
all shared openers, real named-column writes into compatible future state,
refusal without logical/backup/receipt changes, omitted-default constraints,
known forward upgrade to new required schema, process crashes on both sides of
commit, and denial of autocommit/executescript transaction escapes. Existing
migration, readiness, backup expiry, locking and release-graph tests remain in
place. Frozen historical downgrade tests reconstruct their pre-bridge fixture
using `tests/migration_helpers.py`; that helper is not a product downgrade API.

No live database, restart, release, remote operation or retention change is
part of bridge validation.
