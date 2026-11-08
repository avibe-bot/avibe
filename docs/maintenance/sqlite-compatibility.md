# SQLite compatibility epoch

Implementation of section A in [the downtime plan](../plans/sqlite-upgrade-downtime.md).
The bridge, mechanical migration classification, and gated no-copy path share the
same preflight. No-copy is enabled only in a build whose immediately previous
published binary passes the real protocol/reader/writer contract. Builds with an
unverified or pre-protocol predecessor keep conservative upgrade snapshots.

## Change contract

- Revision `20261108_0076` establishes integer `state_meta.compatibility_epoch = 1`.
  Its schema is otherwise unchanged. Entering the bridge from a non-empty
  pre-protocol database always gets a fresh snapshot (clone verification is
  deferred until restore; online-backup copies are verified immediately); a marker created
  by this upgrade cannot opt that same upgrade out. Fresh databases need no snapshot.
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
- `MigrationPreflight.skip_snapshot` omits the copy, **not** the migration. The
  exact pending revision pair remains present and every migration still runs.
  It requires the four release conditions below and already-complete importer
  receipts; missing receipts mean unclassified repair/import work and keep the
  conservative path. A no-copy bootstrap returns without backup validation,
  receipt adoption, import replay, report-only row scans or an added full-file
  integrity scan. Existing transaction and final-schema checks remain.
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

C-alt's projection-aware reader shipped in `gh-v3.2.5rc19` with support for
`{1, 2}` while retaining compatibility traces. Breaking revision `20261108_0080`
activates epoch 2; it always takes the conservative upgrade snapshot on populated
state, even in an activated build. The epoch/stamp commit together, without
backfilling or deleting any history. Only exact supported committed Vibey calls
may then omit their redundant trace, and only with an epoch-2 predecessor proof.
Unmarked/unknown/non-Vibey and routed-scope fallback paths retain single-copy
traces. See [the representation contract](../plans/vibey-tool-activity-projection.md#stage-2-epoch-gated-write-suppression).

**The projection-aware rollback floor is RC19.** Select RC19 or a newer supported
reader without restamping/downgrading. Trace-only older binaries refuse epoch-2
state rather than silently dropping tool activity. An explicit verified restore
for an unsupported older binary still discards all writes since the snapshot;
stop all writers first. No automatic backfill/export or filesystem reclamation
is part of this transition. Epoch 1 alone never authorizes suppression.

## Release boundary (owner decision, 2026-11-08)

**Bridge release (R0)** means the first release of any kind, RC or stable, whose
binaries implement protocol v1. A prerelease establishes the bridge for each
DB it opens; the per-DB integer marker is the authority, not a version number.
`gh-v3.2.5rc19`, published on 2026-11-08, establishes that bridge; later stable
releases carry the same protocol. This implementation cuts no release or version.

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

### One predecessor, one build literal

`scripts/sqlite_predecessor_contract.py` selects the immediately previous
**published** GitHub release by publication time, including RCs and releases
with an unchanged migration graph. It verifies that release's wheel digest,
installs the actual wheel in an isolated environment, and checks the imported
version/package path. It never substitutes current source for that binary.
Every probe process gets a test-owned HOME/Avibe/XDG tree and no caller routing
or credentials. No service is started.

The probe uses real settings, Sessions, Harness records, Message and trace
readers/writers. It upgrades populated predecessor state, writes candidate rows,
then exercises the older binary against same-epoch future state with independent
tables and nullable/defaulted columns. Existing new values must survive whole-row
upserts and load/save; omitted columns must take server defaults. It checks
stored type/JSON interpretation and unknown extensions in extensible metadata,
then reopens with the candidate. A simulated `INSERT OR REPLACE` loss and JSON
loss are negative harness regressions. This does **not** make arbitrary new
closed-schema settings meanings additive: semantic changes still need review
and a breaking boundary.

The existing migration-guard CI job runs this contract. The existing publisher
runs it again immediately before packaging, and `--activate` fills just
`storage.compatibility.VERIFIED_PREDECESSOR_EPOCH` with the verified epoch.
`None` disables the capability; pre-protocol or unsupported predecessors leave
it `None`, with an explicit **not-applicable**, never a passing rollback claim.
The source literal is now `2` after actual RC19 and RC20 wheels passed at epoch 2,
including trace-suppressed canonical tool presentation through Activity and SQL.
The [representation contract](../plans/vibey-tool-activity-projection.md#released-reader-proof)
records the wheel digests and RC18 refusal evidence. The no-copy decision still
requires a proven additive same-epoch path; activation cannot skip the epoch-2
transition's rollback snapshot.
CI rechecks an activated literal against the actual published predecessor;
the publisher independently fills or clears it before packaging.
A failed applicable reader/writer test stops the build. The literal ships in the
wheel/sdist; there is no runtime network check, config switch, per-DB lineage or
separate attestation registry. Local source-fixture tests validate the harness,
not published-predecessor capability.

### Existing backup lifecycle

No-copy creates no backup attempt and changes no prior receipt or expiry clock.
A restart cannot adopt a receipt whose exact target no longer matches the live
stamp. Age maintenance may accept a **known, proven-additive, same-epoch** path
from a ready receipt's target to the live stamp, so additive upgrades do not
freeze otherwise eligible expiry. Failed, unconfirmed, restored and wrong-inode
receipts remain protected; the original 72-hour clock and exact-position count
retention are unchanged.

### Physical verification and restore

Conservative/breaking upgrades still take a fresh rollback copy. A filesystem
clone is made only after checkpointing under SQLite's exclusive writer lock;
its independent COW file preserves the source bytes, including any pre-existing
bad pages. It is **not physically verified at creation**. The existing backup
manifest records `copy_method` and `verification.status = "deferred"`. When COW
is unavailable, the SQLite online-backup path still runs `PRAGMA quick_check`
before publishing its copy and records a creation-time pass.

Neither post-migration lifecycle completion runs a full live-database scan.
Their exact revision checks, `migrated`/`validated` transitions, service readiness,
72-hour expiry clock and first/last-copy position retention remain. In these
receipts, `validated` means application-state validation completed, **not** that
the rollback copy passed a physical scan. The manifest's separate `verification`
field is the latest physical-verification result. Unverified clones may expire
under the same successful-upgrade policy; deferring verification does not extend
retention or guarantee detection of dormant corruption during startup.

`quick_check` is not proof of migration correctness. Migration writes still use
SQLite transactions, per-revision epoch checks and final schema checks. A bad
page encountered by a modern migration causes that transaction to fail and roll
back; unaccessed bad pages may remain undiscovered. The importer retains its own
`integrity_check` when it actually imports JSON/background state. No background
scan, new startup readiness gate or change to historical migration transactions
is introduced here.

Every restore through `storage.backups.restore_sqlite_backup` checks the **staged
candidate** before `_swap_live_database`, even when that copy passed a prior
check. The existing manifest is required to record the latest restore pass or
failure durably before promotion. A failed check leaves the live database and
its WAL/SHM untouched, removes staging files, and refuses that candidate. Its
error names the next older recognized directory backup, ordered by recorded
sequence (legacy timestamps only when no sequence exists), excluding known failed
copies. That is a suggestion, **not a verified or binary-compatible fallback**:
no older backup is automatically restored. Target-storage/access failures are not
proof of corruption: an incomplete attempt preserves the last completed physical
check (pass or failure), or records `incomplete` if none exists. A check that passed
before a target fsync failure retains its physical pass. Such faults neither hide
a healthy candidate nor clear a known corruption result; resolve the storage/access
problem and retry the same copy rather than losing newer writes. With no older
eligible copy, corruption/missing-source errors ask for manual recovery while
retaining the live database and backup evidence.
Stop all writers before restoring; choosing an older point discards newer writes.
As before, a restore attempt revokes prior expiry authority even when it fails;
a successful physical check cannot revive an old readiness/expiry clock.

## Mechanical classification

Prefer additive **expand/contract** changes: add the new representation, dual-write
while old readers remain supported, and remove the old representation in a later
release with the required breaking boundary. In-place rewrites and other breaking
migrations are exceptions; explain why an additive rollout cannot meet the need.

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

Release-cadence dependency: the immediately previous published binary must
contain the bridge and pass the real contract before a no-copy build can activate.
The actual RC19 wheel has passed at epoch 1; a local bridge fixture alone would
not prove a released rollback target. Public English and Chinese documentation
must distinguish the conservative bridge from later verified no-copy releases;
no release is created by this implementation.

## Validation

Primary owner coverage: `tests/test_sqlite_compatibility_epoch.py`. It exercises
all shared openers, real named-column writes into compatible future state,
refusal without logical/backup/receipt changes, omitted-default constraints,
known forward upgrade to new required schema, process crashes on both sides of
commit, and denial of autocommit/executescript transaction escapes. Existing
migration, readiness, backup expiry, locking and release-graph tests remain in
place. Frozen historical downgrade tests reconstruct their pre-bridge fixture
using `tests/migration_helpers.py`; that helper is not a product downgrade API.

`tests/test_sqlite_no_copy.py` owns snapshot omission, conservative fallbacks,
crash/resume, stale-attempt refusal and original-clock expiry across additive
successors. `tests/test_sqlite_predecessor_contract.py` checks predecessor
selection, fixture isolation and loss-detection in the probe; the separately
executed published-wheel probe supplies release evidence.

`tests/test_sqlite_snapshot.py` owns clone/online-copy verification and corrupt-page
restore refusal. `tests/test_state_backups.py` covers staged restore preservation,
write-order fallback hints without automatic restore, and crash/durability
boundaries. `tests/test_sqlite_backup_expiry.py` covers scan-free exact-target
lifecycle completion, fresh restore evidence (including receipt I/O failures),
and unchanged expiry/readiness semantics.

No live database, restart, release, remote operation or retention change is
part of this validation.
