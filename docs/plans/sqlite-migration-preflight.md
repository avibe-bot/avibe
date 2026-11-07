# SQLite migration revision preflight

## Change contract

The migration owner must establish that a stamped database can move forward in
this executable's installed Alembic graph before it creates a rollback copy or
runs any schema repair. The existing per-database migration lock owns this
check and the migration it authorizes.

- Unknown revisions, a requested ancestor, or otherwise incompatible revision
  sets are refused without changing logical database contents or schema, creating
  a backup, or running a schema-repair helper. Existing backup files and lifecycle
  receipts remain unchanged. SQLite's native crash recovery, checkpointing and
  WAL/shared-memory sidecar bookkeeping are allowed; physical byte-invariance of
  the database and its journals is not the contract.
- The error identifies the Python executable, Avibe version and migration code
  location, database path, stored revisions, installed heads, and requested
  target. Its hint names the common newer-database/older-executable mismatch;
  an unknown revision alone cannot prove which binary wrote it.
- Revision identity and ancestry come from Alembic, never lexical/date ordering.
  Every stored revision must be recognized exactly and belong to a forward
  path to the requested target, including branched/merged revision sets.
- Valid upgrades retain the unconditional fresh SQLite backup. A revision stamp
  is not a content fingerprint: a previous attempt may have committed data
  before failing, so a rollback copy cannot be reused merely by stamp.
- Same-head opens create no migration backup. Unversioned/legacy repair and its
  backup behavior remain unchanged. There is no automatic downgrade, restore,
  or restamp.

## Evidence and scope

A field report recorded full-database backups from `20261030_0072` to
`20260821_0060`. An isolated old-source reproduction writes that manifest before
Alembic rejects the unknown stored revision; the manifest's target is an intent,
not evidence of a completed downgrade. Current code also adds repair columns to
an unknown, synthetic future-stamped `agents(id)` table before rejecting it.

The fix belongs in `storage/migrations.py`, before the existing backup/repair
sequence. Read the stamp through the same ordinary SQLite connection as before:
SQLite must recover hot journals so the guard sees the committed revision, not
an uncommitted page image. Neither read-only/immutable modes nor a private
recovery copy are needed. No schema migration or new lock is needed. Existing
older executables cannot be retrofitted by this change; users must update the
launcher that still opens the newer database.

Out of scope: online compaction, terminal-write retry/reconciliation, history
retention, a runtime-wide mixed-version exclusion protocol, and changes to
backup pruning/expiry or explicit restore/downgrade workflows.

## Validation plan

At the migration-owner boundary, prove refusal preserves logical contents,
schema and backup bytes for the future-stamped repairable shape, mixed
known/unknown revisions, ahead-of-target and unrelated branches. Check
diagnostics and exact revision identity. Exercise committed WAL revisions with
open and closed writers, and hot
rollback-journal recovery for both compatible and incompatible stored stamps.
A branched fixture proves valid merges and non-lexical ancestry.
Existing real migration/backup tests cover valid upgrade, fresh-copy retry,
same-head no backup and legacy unversioned repair. Run the focused regression
against the pre-fix code to prove it fails for writes-before-refusal, then run
migration, readiness, backup and lock regressions plus changed-file lint.

## Validation evidence

- The future-stamped `agents(id)` regression fails against the original backup
  decision from `952859064`: the logical dump gains repair columns and the backup
  directory/lifecycle receipt changes before Alembic rejects the stamp.
- The six-file migration, preflight, readiness, backup, expiry and migration-lock
  suite passes **271 tests**, including the new open/closed WAL and actual
  hot-journal subprocess cases. Changed-file Ruff and `git diff --check` pass.
- A local synthetic **6,105,247,744-byte** database with an unknown stamp took
  **10.122 s** on the baseline and wrote a full-size backup before failing. The
  fixed path refused it in **0.057 s**, made no additional backup and preserved
  its contents. This is an isolated local measurement, not a cross-machine
  latency guarantee; the large test database and backup were removed.

Only isolated test state is used. No live database, service restart, or remote
regression deployment is part of this change.
