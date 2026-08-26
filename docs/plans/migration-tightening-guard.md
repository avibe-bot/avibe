# Migration Tightening Guard

## Background

The release guard proves that representative populated databases survive the current
migration graph, but row seeding cannot cover every value a released SQLite schema can
hold. Issue #1577 asks for a universal gate over schema-tightening migrations instead of
another fixture expansion.

## Goal

For every distinct released schema and every current migration Alembic would apply to it,
require each newly introduced uniqueness, NOT NULL, or CHECK guarantee to be either implied
by the incoming schema or preceded by an explicit data-establishing step in that migration.
When a migration replaces a column, its backfill must precede removal of the source column.
The test must derive its migration coverage from the release graph, not from a migration
allowlist.

## Design

- Reuse the release universe, migration identity, graph parsing, and released-version
  extraction in `scripts/migration_release_guard.py`.
- Materialize each distinct released empty schema with its shipped Alembic graph, then use
  Alembic's current upgrade plan as the coverage denominator.
- Compare before/after schemas with SQLite PRAGMAs and SQLAlchemy's SQLite CHECK parser.
  This avoids implementing a second SQLite schema interpreter.
- Analyze the current migration source statically for ordered deduplication, relevant
  backfill, data validation, table replacement, and schema-establishing column additions.
- Fail closed when an applied migration or a tightening cannot be analyzed.

This deliberately differs from the issue's initial suggestion that the analyzer should not
run Alembic at all. Arbitrary Python migration bodies and SQLite batch table rebuilds make a
complete static schema interpreter another schema model. Running migrations in a temporary
empty database only to obtain authoritative per-revision schema snapshots keeps the
establishing-step verdict static while delegating graph traversal and SQLite schema semantics
to the systems that own them.

## Validation

- Unit properties for unique indexes/constraints, direct NOT NULL additions, nullable to
  NOT NULL changes, CHECK constraints, and backfills from columns removed in the same
  migration.
- A release-history property requiring every planned `(released graph, migration)` pair to
  be analyzed and every observed tightening to be justified.
- Existing migration release guard tests and Ruff.
