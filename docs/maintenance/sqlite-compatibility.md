# SQLite compatibility epoch

Implementation of section A in [the downtime plan](../plans/sqlite-upgrade-downtime.md).
The bridge and mechanical migration classification are implemented. Snapshot-free
upgrade activation and the released-predecessor reader contract remain separate
follow-up work; all upgrade snapshots are still conservative.

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
  DDL/data, Alembic stamp and epoch commit together. Each revision validates the
  epoch against its literal **source** declaration before its own commit, even
  at an intermediate head; a mutable Python module attribute is not authority.
  The final head's required schema is validated before commit, without a full-file scan. Transaction
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
The owner clarified the gate on 2026-11-08: all four conditions must hold:

1. A valid marker was already present **at the start** of this upgrade. A marker
   first written during this attempt never counts.
2. The immediately previous published binary has passed the protocol and real
   reader/writer contract test. RCs count as published binaries.
3. The exact Alembic forward path is known.
4. Every pending migration is mechanically proven additive.

This is released-predecessor capability evidence, not a claim about which binary
last wrote this particular database. There is no generation field, per-database
lineage record, or semver threshold. Missing evidence keeps snapshots enabled.
Rolling back to a pre-protocol binary requires the bridge-time backup with all
writers stopped; restoring it discards later writes. **Once that backup expires,
rollback to a pre-bridge binary is unsupported.**

## Mechanical classification

`storage.migration_policy` interprets a small literal Python grammar and rebuilds
the corresponding Alembic operation objects without executing migration code.
The author must declare `MIGRATION_COMPATIBILITY = "additive"` or `"breaking"`;
only a proven additive body can opt in. Historical `MIGRATION_SAFETY` remains a
separate data-safety attestation, not a compatibility proof.

The canonical additive syntax uses `from alembic import op`, optional
`import sqlalchemy as sa`, literal revision metadata, and straight-line calls:

```python
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_index("ix_example_name", "example", ["name"], unique=False)
    op.add_column("example", sa.Column("note", sa.Text(), nullable=True))
```

The finite allowlist is non-unique indexes on plain columns, independent new
column-only tables, and nullable or non-null literal-server-default columns.
Existing-table uniqueness, foreign keys, checks, generated columns, expression
indexes, rowid-alias-shadowing columns, triggers, raw SQL, helpers, conditionals,
rebuild/drop/rename, unknown constructors/options and module-level work are not
proven. Type sizes/precision must be literal integers: SQLAlchemy accepting a
string constructor argument does not make its emitted DDL safe. A function or
module annotation/default that could run code is also not proof. Extend this allowlist
only with actual operation and old-writer tests, not a label exemption.

The existing release guard still checks all shipped graphs and populated
upgrades. In addition, new revisions must carry a literal classification; an
unproved additive label fails CI. Breaking revisions must declare a positive
literal `COMPATIBILITY_EPOCH` greater than both their ancestors' epochs and the
released graph's highest epoch, including semantic changes without DDL. The
whole-database boundary cannot be reused by a new breaking sibling branch.
Ancestry/dependency labels resolve through Alembic's own `RevisionMap`; augmented,
conditional or other epoch rebindings are not literal declarations. Additive
revisions do not declare an epoch boundary. Released migrations are not relabeled.
None of this gate skips a runtime snapshot by itself.

Remaining A work:

1. The immediately previous release's real reader/writer contract test. Before
   the bridge is released, that previous release does not implement this protocol
   and is **not** a supported binary rollback target.
2. Additive no-copy activation with valid existing bridge marker only, plus
   exact-attempt backup/expiry tests and synthetic-volume latency evidence.
   No receipt adoption or expiry-clock resets on the no-copy path.
3. Align public English/Chinese upgrade documentation for the bridge and its
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
