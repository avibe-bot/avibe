from __future__ import annotations

from logging.config import fileConfig
import sqlite3

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, pool

from storage.compatibility import BRIDGE_REVISION
from storage.models import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode = WAL")
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.exec_driver_sql("PRAGMA busy_timeout = 5000")
        connection.commit()

        context.configure(connection=connection, target_metadata=target_metadata)
        migration_fn = context.get_context().opts["fn"]
        script = ScriptDirectory.from_config(config)
        installed = {item.revision for item in script.walk_revisions()}
        modern = (
            {item.revision for item in script.walk_revisions(base=BRIDGE_REVISION)}
            if BRIDGE_REVISION in installed else set()
        )

        def transactional_steps(heads, migration_context):
            # SQLite's legacy driver does not BEGIN for DDL. Start the real
            # transaction before Alembic enters each new migration, then let
            # Alembic commit its work and version stamp together. Released
            # pre-bridge revisions retain their original transaction behavior.
            for step in migration_fn(heads, migration_context):
                if getattr(getattr(step, "revision", None), "revision", None) in modern:
                    raw = connection.connection.driver_connection
                    if raw.in_transaction:
                        raise RuntimeError("new migration unexpectedly entered with an open SQLite transaction")
                    referenced_tables = {
                        fk[2].casefold()
                        for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                        for fk in raw.execute('PRAGMA foreign_key_list("' + name.replace('"', '""') + '")')
                    }
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    original = step.migration_fn

                    def atomic_migration(*args, _original=original, **kwargs):
                        def authorize(action, arg1, arg2, _database, _source):
                            if action in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                                          sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
                                return sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_DROP_TABLE and arg1.casefold() in referenced_tables:
                                # Batch rebuild's DROP would cascade into child
                                # data while foreign_keys remains enabled.
                                return sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_PRAGMA and arg2 is not None and arg1.lower() not in {
                                "table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
                                "foreign_key_list",
                            }:
                                return sqlite3.SQLITE_DENY
                            return sqlite3.SQLITE_OK

                        # Do not let helpers/autocommit/executescript split new
                        # DDL/data from its stamp and epoch. Released rebuilds
                        # are unchanged; a new rebuild must be designed to work
                        # inside this boundary, not opt out as "breaking".
                        raw.set_authorizer(authorize)
                        try:
                            return _original(*args, **kwargs)
                        finally:
                            # None is only supported here from Python 3.11.
                            # An allow-all callback also resets safely on 3.10.
                            raw.set_authorizer(lambda *_: sqlite3.SQLITE_OK)

                    step.migration_fn = atomic_migration
                yield step

        def validate_epoch_schema(*, ctx, step, heads, run_args):
            if not step.is_upgrade or not heads.intersection(modern):
                return
            from storage.compatibility import read_compatibility_epoch, require_supported_compatibility_epoch
            from storage.migrations import alembic_config, declared_compatibility_epoch, _compatible_schema_problem

            raw = connection.connection.driver_connection
            epoch = read_compatibility_epoch(raw)
            # Every modern revision is its own transaction. Validate its epoch
            # before each commit, not only the final head: a later failure/crash
            # must not expose a restrictive intermediate schema under an old epoch.
            if epoch != declared_compatibility_epoch(script, heads):
                raise RuntimeError("compatibility_epoch does not match the committed schema revision")
            # The release-graph guard drives historical version_locations with
            # today's env.py. A historical head must not require today's new
            # columns; only the actual installed head owns this schema check.
            if heads == set(script.get_heads()) == set(ScriptDirectory.from_config(alembic_config()).get_heads()):
                require_supported_compatibility_epoch(epoch)
                problem = _compatible_schema_problem(raw)
                if problem:
                    raise RuntimeError(problem)

        context.configure(
            connection=connection, target_metadata=target_metadata, fn=transactional_steps,
            on_version_apply=validate_epoch_schema,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
