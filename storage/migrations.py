from __future__ import annotations

import os
import sqlite3
import logging
import re
import sys
import threading
from dataclasses import dataclass, replace
from functools import lru_cache
from contextlib import closing
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util import CommandError

from config import paths
from storage.backups import (
    begin_sqlite_backup_migration,
    complete_sqlite_backup_migration,
    create_sqlite_migration_backup,
    prune_state_backups,
)
from storage.compatibility import (
    BRIDGE_REVISION, VERIFIED_PREDECESSOR_EPOCH, CompatibilityEpochError,
    read_compatibility_epoch, require_supported_compatibility_epoch,
)
from storage.db import create_sqlite_engine, sqlite_url
from storage.migration_policy import classify_migration, declared_migration_epoch
from storage.lock import MigrationFileLock, migration_lock_path_for


logger = logging.getLogger(__name__)

# Alembic's EnvironmentContext installs module-level proxies while an upgrade
# runs. Concurrent store initialization can otherwise tear down another
# thread's proxy and surface errors such as KeyError("config"). This owns that
# hazard and nothing else: the proxies are per interpreter, not per database, so
# it must stay global, and it says nothing about other processes.
_MIGRATION_LOCK = threading.RLock()

INITIAL_REVISION = "20260501_0001"
# Stamp FLOOR for head-shaped unversioned databases, deliberately NOT head: after
# `_stamp_existing_initial_schema` stamps it, `_run_migrations_locked` upgrades to head,
# so every revision after this one replays on a schema that already looks like head.
# Each such migration must therefore stay idempotent/replay-safe against head shape
# (e.g. `drop index if exists` before `create index`) rather than assume a virgin table.
LATEST_SCHEMA_REVISION = "20260622_0023"
REMOVE_LEGACY_DEFAULT_AGENT_REVISION = "20260530_0008"
ALLOW_DEV_STATE_MIGRATION_ENV = "AVIBE_ALLOW_DEV_STATE_MIGRATION"
INITIAL_TABLES = {
    "state_meta",
    "agents",
    "scopes",
    "scope_settings",
    "auth_codes",
    "agent_sessions",
    "runtime_records",
}


class UnsafeDefaultStateMigrationError(RuntimeError):
    """Raised when source-checkout code would migrate the user's default state."""


class IncompatibleDatabaseRevisionError(RuntimeError):
    """The stored revisions or reader/writer epoch are incompatible."""


@dataclass(frozen=True)
class MigrationPreflight:
    """One decision shared by migration, bootstrap, explicit stores and readiness."""

    compatible_open: bool = False
    compatibility_epoch: int | None = None
    backup_revisions: tuple[set[str], set[str]] | None = None
    skip_snapshot: bool = False
    recovered_hot_journal: bool = False


HEAD_TABLES = INITIAL_TABLES | {
    "run_definitions",
    "agent_runs",
    "show_pages",
    "show_page_access_entries",
    "messages",
    "message_deliveries",
    "message_search_dirty",
    "message_search_documents",
    "message_search_state",
    "session_turns",
    "session_turn_replies",
    "session_delegations",
    "agent_events",
    "session_digests",
    "session_cache_state",
    "skill_usage_daily",
    "skill_catalogs",
    "show_session_events",
    "media_objects",
    "media_object_references",
    "web_push_subscriptions",
    "project_access_policies",
    "project_access_bindings",
    "remote_access_authorizations",
    "vault_secrets",
    "vault_requests",
    "vault_grants",
    "vault_audit",
    "vault_audit_access_snapshots",
    "vault_auth_factors",
    "vault_operation_challenges",
    "resource_access_policies",
    "resource_access_groups",
}
PRE_SHOW_SESSION_EVENTS_HEAD_TABLES = INITIAL_TABLES | {
    "run_definitions",
    "agent_runs",
    "show_pages",
    "messages",
}
PRE_SHOW_SESSION_EVENTS_REQUIRED_COLUMNS = {
    "agents": {"enabled"},
    "scope_settings": {"agent_name"},
    "agent_sessions": {"agent_id", "agent_name", "visibility", "pinned"},
    "messages": {"type", "source"},
    "run_definitions": {
        "deleted_at",
        "definition_type",
        "agent_name",
        "session_policy",
        "message",
        "message_payload_json",
        "last_run_id",
        "retired_at",
    },
    "agent_runs": {
        "definition_id",
        "source_kind",
        "source_actor",
        "parent_run_id",
        "agent_name",
        "agent_id",
        "agent_backend",
        "model",
        "reasoning_effort",
        "session_policy",
        "message",
        "message_payload_json",
        "result_text",
        "result_payload_json",
        "message_ids_json",
        "callback_session_id",
        "callback_status",
        "callback_error",
        "callback_run_id",
        "callback_completed_at",
        "callback_terminal_turn_id",
        "cancel_requested",
        "cancel_requested_at",
    },
}
HEAD_REQUIRED_COLUMNS = PRE_SHOW_SESSION_EVENTS_REQUIRED_COLUMNS | {
    "run_definitions": PRE_SHOW_SESSION_EVENTS_REQUIRED_COLUMNS["run_definitions"]
    | {"retirement_reason"},
    "agent_sessions": PRE_SHOW_SESSION_EVENTS_REQUIRED_COLUMNS["agent_sessions"]
    | {
        "composer_draft_text",
        "composer_draft_updated_at",
    },
    "message_deliveries": {
        "message_id",
        "priority",
        "state",
        "dispatch_text",
        "delivery_history_json",
        "version",
    },
    "messages": PRE_SHOW_SESSION_EVENTS_REQUIRED_COLUMNS["messages"] | {"context_seq"},
    "agent_events": {"context_seq"},
    "session_turns": {
        "initial_delivery_id",
        "state",
        "native_turn_id",
        "control_state",
        "terminal_outcome",
        "settled_by",
        "version",
    },
}
HEAD_ONLY_REQUIRED_COLUMNS = {
    "session_cache_state": {"session_id", "provider", "model", "record_json"},
    "session_delegations": {
        "sender_session_id", "recipient_session_id", "message_id", "run_id", "state", "notice_run_id", "updated_at",
    },
    "vault_secrets": {"fields"},
    "show_pages": {"access_mode", "access_revision", "share_id", "offline_at"},
    "show_page_access_entries": {
        "page_id",
        "kind",
        "value",
        "organization_id",
        "created_at",
    },
    "web_push_subscriptions": {"device_id", "provider_invalidated_at"},
    "remote_access_authorizations": {
        "instance_id",
        "subject",
        "email",
        "scope_kind",
        "scope_ref",
        "authorization_state",
        "claims_json",
        "expires_at",
        "created_at",
        "last_checked_at",
        "updated_at",
    },
    "vault_requests": {"callback_status"},
    "vault_grants": {"agent_ready", "agent_ready_at"},
    "vault_auth_factors": {"credential_id", "public_key", "alg", "sign_count"},
    "vault_operation_challenges": {"challenge_hash", "rp_id", "origin", "expires_at", "consumed_at"},
    "resource_access_policies": {
        "organization_id",
        "owner_user_id",
        "owner_email",
        "access_level",
        "policy_revision",
        "last_applied_control_plane_revision",
        "created_by_user_id",
        "updated_by_user_id",
    },
    "resource_access_groups": {"organization_id", "group_id"},
}
UNRELEASED_OLD_INITIAL_TABLES = [
    "session_messages",
    "chat_sessions",
    "channel_settings",
    "guild_settings",
    "guild_policies",
    "user_settings",
    "bind_codes",
    "agent_session_bindings",
    "active_threads",
    "active_polls",
    "processed_messages",
    "discovered_chats",
    "scopes",
    "schema_meta",
    "alembic_version",
]
# The members above that no release could have produced, which is what makes the presence
# of one evidence of an unreleased build. `scopes` is in INITIAL_TABLES and every stamped
# database has `alembic_version` -- the check below requires it a few lines earlier -- so
# testing the full list proved nothing: any database short of INITIAL_TABLES matched, and
# a released one then had its `scopes` and its stamp dropped out from under it. Derived
# rather than written out, so a table added to INITIAL_TABLES leaves this correct.
UNRELEASED_ONLY_INITIAL_TABLES = [
    table
    for table in UNRELEASED_OLD_INITIAL_TABLES
    if table not in INITIAL_TABLES and table != "alembic_version"
]


def alembic_dir() -> Path:
    return Path(__file__).resolve().parent / "alembic"


def set_literal_main_option(cfg: Config, name: str, value: str) -> None:
    """Store `value` in Alembic's config so it reads back unchanged.

    Alembic keeps main options in a ConfigParser with `%` interpolation, so a raw
    `%` is rejected on write, or, shaped like `%(here)s`, silently replaced on
    read with a different path. Paths reach here verbatim: a directory can contain a
    literal `%`, and SQLAlchemy 2.1 percent-encodes every non-ASCII character when
    rendering a URL, so a state directory under a Chinese user name became
    `%E9%...` and migrations failed before they started. Escaping is the
    documented way to pass a literal through; interpolation undoes it on read.
    """
    cfg.set_main_option(name, value.replace("%", "%%"))


def alembic_config(db_path: Path | None = None) -> Config:
    cfg = Config()
    set_literal_main_option(cfg, "script_location", str(alembic_dir()))
    set_literal_main_option(cfg, "sqlalchemy.url", sqlite_url(db_path or paths.get_sqlite_state_path()))
    return cfg


@lru_cache(maxsize=4)
def _installed_script_directory(location: str, versions_mtime: int) -> ScriptDirectory:
    # An installed binary's graph is immutable. The directory mtime also keeps
    # isolated source/check-out tests honest when a new revision file is added.
    cfg = Config()
    set_literal_main_option(cfg, "script_location", location)
    return ScriptDirectory.from_config(cfg)


def _script_directory(cfg: Config) -> ScriptDirectory:
    location = cfg.get_main_option("script_location")
    if not cfg.get_main_option("version_locations") and location and Path(location).is_dir():
        return _installed_script_directory(location, (Path(location) / "versions").stat().st_mtime_ns)
    return ScriptDirectory.from_config(cfg)


def declared_compatibility_epoch(script: ScriptDirectory, revisions: set[str]) -> int:
    """Epoch at an exact bridge-or-later revision set, using the Alembic graph."""
    epochs = []
    for item in script.iterate_revisions(tuple(revisions), BRIDGE_REVISION, inclusive=True):
        try:
            epoch = declared_migration_epoch(Path(item.path).read_text(encoding="utf-8"), strict=True)
        except (ValueError, SyntaxError) as exc:
            raise CompatibilityEpochError(f"invalid compatibility_epoch declaration in {item.revision}: {exc}") from exc
        # Module-level annotations/defaults and opaque breaking helpers can
        # mutate module attributes. The literal source is the contract that
        # commit validation must enforce, regardless of those Python effects.
        epochs.append(epoch or 1)
    return max(epochs)


def is_proven_additive_successor(
    current_revisions: set[str], target_revisions: set[str], epoch: int | None,
    *, script: ScriptDirectory | None = None,
) -> bool:
    """Exact same-epoch forward path, shared by no-copy admission and old-copy expiry."""
    if epoch is None or not current_revisions or current_revisions == target_revisions:
        return False
    try:
        script = script or _script_directory(alembic_config())
        known = {item.revision for item in script.walk_revisions(base=BRIDGE_REVISION)}
        if not current_revisions.issubset(known) or not target_revisions or not target_revisions.issubset(known):
            return False
        pending = tuple(script.iterate_revisions(tuple(target_revisions), tuple(current_revisions)))
        return (
            bool(pending)
            and declared_compatibility_epoch(script, current_revisions) == epoch
            and declared_compatibility_epoch(script, target_revisions) == epoch
            and all(classify_migration(item.path).permitted == "additive" for item in pending)
        )
    except (CommandError, RevisionError, CompatibilityEpochError, OSError):
        return False


def run_migrations(
    db_path: Path | None = None,
    *,
    revision: str = "head",
    prune_backups_after_upgrade: bool = True,
) -> MigrationPreflight:
    """Bring one SQLite state database to `revision`, or open a compatible future schema.

    Exclusion belongs here rather than at the call sites, because "at most one
    migrator per database" is a property of the database, and a call site can
    only promise it for itself. It was previously promised by `ensure_sqlite_state`
    alone, which left every other entry point -- the discovery helpers, a store
    constructed with an explicit `db_path`, the background-table bootstrap --
    running `command.upgrade` against a file another process could be upgrading
    at the same moment. The controller and the Web UI are separate processes on
    one state directory, so that pairing is the ordinary case, not a corner.

    Both locks are load-bearing and neither substitutes for the other: the file
    lock is machine-wide and per database, and `_MIGRATION_LOCK` is per
    interpreter and global, guarding Alembic's module-level proxies.

    The file lock is taken first, and always first, which is what makes the pair
    deadlock-free. Taking the global one first would also let a thread hold every
    database's migrations in this process while it waits on a foreign process --
    a remote stall propagating into unrelated local work.

    The wait is deliberately unbounded. A file lock is released by the OS when
    its holder dies, so the only way to wait forever is for a live process to
    still be migrating -- and a migration's duration is bounded by the data, not
    by anything we could name here. Any deadline would be a guess that turns a
    slow, correct upgrade into a failed startup for every other entry point.
    Waiting is logged with the holder's pid so the wait stays diagnosable.
    """

    target_db = (db_path or paths.get_sqlite_state_path()).expanduser().resolve()
    guard_source_checkout_default_state_migration(target_db)
    with MigrationFileLock(migration_lock_path_for(target_db), timeout_seconds=None):
        with _MIGRATION_LOCK:
            return _run_migrations_locked(
                target_db,
                revision=revision,
                prune_backups_after_upgrade=prune_backups_after_upgrade,
            )


def _run_migrations_locked(
    target_db: Path,
    *,
    revision: str,
    prune_backups_after_upgrade: bool,
) -> MigrationPreflight:
    cfg = alembic_config(target_db)
    from storage.migration_space import InsufficientMigrationSpaceError

    decision = preflight_for_write(target_db, cfg=cfg, revision=revision)
    if decision.compatible_open or (decision.compatibility_epoch is not None and decision.backup_revisions is None):
        return decision
    backup_revisions = decision.backup_revisions
    backup_attempt: str | None = None
    if backup_revisions is not None and not decision.skip_snapshot:
        _current_revisions, target_revisions = backup_revisions
        try:
            backup_path = create_sqlite_migration_backup(target_db, to_revisions=target_revisions)
        except InsufficientMigrationSpaceError as exc:
            if decision.recovered_hot_journal:
                raise InsufficientMigrationSpaceError(exc.plan, recovered=True) from exc
            raise
        backup_attempt = begin_sqlite_backup_migration(target_db, backup_path)
        logger.info("Pre-migration SQLite rollback point at %s", backup_path)
    if decision.compatibility_epoch is None:
        _reset_unreleased_initial_schema_drift(target_db)
        _repair_unreleased_head_schema_drift(target_db)
        _stamp_existing_initial_schema(target_db, cfg)
    command.upgrade(cfg, revision)
    if backup_attempt is not None:
        try:
            complete_sqlite_backup_migration(target_db, backup_attempt)
        except OSError:
            logger.warning("SQLite migration completed without expiry evidence; retaining backups", exc_info=True)
    if prune_backups_after_upgrade and not decision.skip_snapshot:
        prune_state_backups(target_db.parent / "backups", json_retention=None)
    return decision


def _needs_hot_journal_recovery(exc: sqlite3.OperationalError, db_path: Path) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code == 776  # SQLITE_READONLY_ROLLBACK; Python 3.10 has no named code.
    # Python 3.10 lacks extended error attributes. Never treat generic busy,
    # corrupt or readonly failures as permission to open read/write.
    if str(exc) != "attempt to write a readonly database":
        return False
    try:
        with db_path.with_name(db_path.name + "-journal").open("rb") as journal:
            return journal.read(8) == b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
    except OSError:
        return False


def preflight_for_write(db_path: Path, *, cfg: Config | None = None, revision: str = "head") -> MigrationPreflight:
    """RO admission first; recovery is a separately admitted mutation phase."""
    try:
        decision = preflight_database(db_path, cfg=cfg, revision=revision, read_only=True)
    except sqlite3.OperationalError as exc:
        if not _needs_hot_journal_recovery(exc, db_path):
            raise
        from storage.migration_space import plan_space

        with MigrationFileLock(migration_lock_path_for(db_path), timeout_seconds=None):
            # No committed schema can be read until SQLite rolls the journal
            # back. Admit that recovery with a filesystem-only generic policy.
            # It is NOT a bound on every possible later migration's reserve.
            plan_space(db_path, migration=True).require()
            # Keep the generic policy for this entire recovery attempt. A
            # policy switch after recovery could refuse solely due to a larger
            # coordinated reserve, despite unchanged available capacity.
            decision = replace(preflight_database(db_path, cfg=cfg, revision=revision), recovered_hot_journal=True)
    require_upgrade_space(db_path, decision)
    return decision


def require_upgrade_space(db_path: Path, decision: MigrationPreflight):
    """Admission only for a pending copy-backed migration, before RW opens.

    Rechecked under the migration lock before copying. Compatible/current and
    proven no-copy opens keep their existing low-space behavior.
    """
    if decision.compatible_open or decision.backup_revisions is None or decision.skip_snapshot:
        return None
    from storage.migration_space import plan_space

    current, target = decision.backup_revisions
    plan = plan_space(db_path, migration=True, coordinated=(
        not decision.recovered_hot_journal and current == {"20261112_0088"} and target == {"20261114_0089"}
    )).require(recovered=decision.recovered_hot_journal)
    logger.info("SQLite upgrade space admission: %s", plan.to_dict())
    return plan


def preflight_database(
    db_path: Path, *, cfg: Config | None = None, revision: str = "head", read_only: bool = False
) -> MigrationPreflight:
    """Refuse incompatible state before backup, repair, import or index rebuild.

    The caller holds the existing migration lock when it will mutate state.
    Compatible-open is deliberately read-only *initialization*, not a read-only
    database: supported application readers/writers may operate afterwards.
    """
    if not db_path.exists() or db_path.stat().st_size == 0:
        return MigrationPreflight()
    cfg = cfg or alembic_config(db_path)
    script = _script_directory(cfg)
    installed_heads = set(script.get_heads())
    target_revisions: set[str] = set()
    current_revisions: set[str] = set()
    skip_snapshot = False
    try:
        target_revisions = {item.revision for item in script.get_revisions(revision)}
        # An ordinary connection preserves SQLite's hot-journal recovery. BEGIN
        # makes marker, stamp and schema observations one committed snapshot.
        database = f"{db_path.resolve().as_uri()}?mode=ro" if read_only else db_path
        with closing(sqlite3.connect(database, uri=read_only)) as conn:
            conn.execute("BEGIN")
            tables = _table_names(conn)
            current_revisions = (
                {str(row[0]) for row in conn.execute("select version_num from alembic_version")}
                if "alembic_version" in tables else set()
            )
            epoch = read_compatibility_epoch(conn)
            installed_revisions = {item.revision for item in script.walk_revisions()}
            unknown = current_revisions - installed_revisions
            if unknown and current_revisions != {""}:
                # Full repository revision IDs only. Never treat a symbolic or
                # partial Alembic identifier, or mixed known/unknown heads, as a
                # future revision. No ordering is inferred from these digits.
                if (
                    epoch is None or revision != "head" or len(current_revisions) != 1
                    or not re.fullmatch(r"\d{8}_\d{4}", next(iter(unknown)))
                ):
                    raise RevisionError("Stored revisions are not in the installed migration graph")
                require_supported_compatibility_epoch(epoch)
                problem = _compatible_schema_problem(conn)
                if problem:
                    raise CompatibilityEpochError(problem)
                return MigrationPreflight(compatible_open=True, compatibility_epoch=epoch)
            if current_revisions and current_revisions != {""}:
                tuple(script.iterate_revisions(tuple(target_revisions), tuple(current_revisions)))
            if epoch is not None:
                if BRIDGE_REVISION not in installed_revisions or not current_revisions:
                    raise CompatibilityEpochError("compatibility_epoch has no bridge revision")
                bridge_descendants = {item.revision for item in script.walk_revisions(base=BRIDGE_REVISION)}
                if not current_revisions.issubset(bridge_descendants):
                    raise CompatibilityEpochError("compatibility_epoch precedes its bridge schema")
                expected_epoch = declared_compatibility_epoch(script, current_revisions)
                if epoch != expected_epoch:
                    raise CompatibilityEpochError(
                        f"compatibility_epoch {epoch} does not match stored revision's epoch {expected_epoch}"
                    )
                # A known forward upgrade may legitimately lack this binary's
                # new columns until its pending migrations run. Check the
                # required application schema only when opening current head.
                if current_revisions == installed_heads:
                    require_supported_compatibility_epoch(epoch)
                    problem = _compatible_schema_problem(conn)
                    if problem:
                        raise CompatibilityEpochError(problem)
            elif BRIDGE_REVISION in installed_revisions:
                bridge_descendants = {item.revision for item in script.walk_revisions(base=BRIDGE_REVISION)}
                if current_revisions & bridge_descendants:
                    raise CompatibilityEpochError("bridge schema is missing compatibility_epoch")
            if (
                epoch is not None and epoch == VERIFIED_PREDECESSOR_EPOCH
                and is_proven_additive_successor(current_revisions, target_revisions, epoch, script=script)
            ):
                # Incomplete importer work is unclassified, not an additive DDL
                # batch. Observe its existing receipts on this same snapshot.
                from storage.importer import completed_sqlite_imports

                skip_snapshot = completed_sqlite_imports(conn)
    except (CommandError, RevisionError, CompatibilityEpochError) as exc:
        from vibe import __version__

        raise IncompatibleDatabaseRevisionError(
            f"Refusing SQLite migration for {db_path}: {exc}. "
            f"Executable: {sys.executable}; Avibe version: {__version__}; "
            f"migration code: {Path(__file__).resolve()}; "
            f"stored revisions: {sorted(current_revisions)}; "
            f"installed heads: {sorted(installed_heads)}; "
            f"requested target: {revision!r} ({sorted(target_revisions)}).\n"
            "Hint: a newer Avibe may have migrated this database; this executable may be older, "
            "or the requested target is incompatible. Use a matching/newer Avibe executable. "
            "Do not restamp or lower compatibility_epoch. Across epochs or to a pre-bridge binary, "
            "stop all writers and explicitly restore a verified backup; restoring discards writes "
            "made since that snapshot. Prefer forward repair."
        ) from exc
    backup = (
        (current_revisions, target_revisions)
        if tables - {"alembic_version"} and current_revisions != target_revisions else None
    )
    return MigrationPreflight(compatibility_epoch=epoch, backup_revisions=backup, skip_snapshot=skip_snapshot)


def _compatible_schema_problem(conn: sqlite3.Connection) -> str | None:
    """Bounded schema inspection, never a repair or a full-database scan."""
    from storage.models import metadata

    tables = _table_names(conn)
    for table in metadata.tables.values():
        if table.name not in tables:
            return f"compatible schema is missing table {table.name}"
        columns = {row[1]: row for row in conn.execute(f'PRAGMA table_info("{table.name}")')}
        missing = set(table.columns.keys()) - columns.keys()
        if missing:
            return f"compatible schema is missing {table.name} columns: {sorted(missing)}"
        for name in columns.keys() - set(table.columns.keys()):
            column = columns[name]
            if column[3] and (column[4] is None or str(column[4]).strip("() ").upper() == "NULL"):
                return f"compatible schema requires old writers to supply new column {table.name}.{name}"
    # INDEXED BY is a correctness dependency, not an optional performance hint.
    for table, index in (
        ("messages", "ix_messages_turn_id"),
        ("agent_events", "ix_agent_events_turn_sequence_id"),
        ("agent_events", "ix_agent_events_session_type_context_seq"),
        ("message_deliveries", "ix_message_deliveries_turn"),
        ("message_deliveries", "ix_message_deliveries_recovery_owner"),
    ):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND name = ?", (table, index)
        ).fetchone():
            return f"compatible schema is missing required index {index}"
    return None


def background_tables_ready(db_path: Path | None = None) -> bool:
    target_db = (db_path or paths.get_sqlite_state_path()).expanduser().resolve()
    if not target_db.exists():
        return False
    try:
        preflight_database(target_db, read_only=True)
    except IncompatibleDatabaseRevisionError:
        return False
    except sqlite3.OperationalError as exc:
        if _needs_hot_journal_recovery(exc, target_db):
            return False  # Writable migration admission owns crash recovery.
        raise
    with closing(sqlite3.connect(f"{target_db.as_uri()}?mode=ro", uri=True)) as conn:
        tables = _table_names(conn)
        return _head_schema_ready(conn, tables)


def initialize_background_tables(db_path: Path | None = None) -> None:
    target_db = db_path or paths.get_sqlite_state_path()
    run_migrations(target_db)


def ensure_background_indexes(db_path: Path | None = None) -> None:
    """Repair head indexes even when the tables already pass readiness checks.

    ``metadata.create_all`` can produce every required table and column without
    migration-owned indexes. Such a database correctly skips schema migration,
    so store construction must still enter the index repair path. Correct indexes
    are detected byte-for-byte against their owning migration DDL and are not
    rebuilt on ordinary opens.
    """

    target_db = (db_path or paths.get_sqlite_state_path()).expanduser().resolve()
    if not target_db.exists():
        return
    if preflight_database(target_db, read_only=True).compatibility_epoch is not None:
        # Epoch schemas are migration-owned, never repaired by old helpers.
        return
    with closing(sqlite3.connect(f"{target_db.as_uri()}?mode=ro", uri=True)) as conn:
        tables = _table_names(conn)
        if not {"run_definitions", "agent_runs"}.issubset(tables) or _agent_runs_managed_indexes_ready(conn):
            return
    with MigrationFileLock(migration_lock_path_for(target_db), timeout_seconds=None):
        if preflight_database(target_db, read_only=True).compatibility_epoch is not None:
            return
        with sqlite3.connect(target_db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not _agent_runs_managed_indexes_ready(conn):
                _ensure_agent_runs_managed_indexes(conn)
            conn.commit()


def guard_source_checkout_default_state_migration(db_path: Path) -> None:
    if _env_flag_enabled(ALLOW_DEV_STATE_MIGRATION_ENV):
        return
    if not _running_from_source_checkout():
        return
    if not _is_default_user_state_db(db_path):
        return

    source_root = Path(__file__).resolve().parents[1]
    raise UnsafeDefaultStateMigrationError(
        "Refusing to run SQLite migrations from an Avibe source checkout "
        f"against the default user state DB at {db_path.expanduser().resolve()}. "
        f"Source root: {source_root}. "
        "Set AVIBE_HOME to an isolated development directory before running "
        "worktree/source CLI commands. If you intentionally need to migrate the "
        f"default local state from source, set {ALLOW_DEV_STATE_MIGRATION_ENV}=1."
    )


def guard_source_checkout_default_state_bootstrap() -> None:
    guard_source_checkout_default_state_migration(paths.get_sqlite_state_path())


def _running_from_source_checkout() -> bool:
    source_root = Path(__file__).resolve().parents[1]
    return (source_root / "pyproject.toml").exists() and (source_root / "storage" / "migrations.py").exists()


def _is_default_user_state_db(db_path: Path) -> bool:
    target = db_path.expanduser().resolve()
    return target in _default_user_state_db_candidates()


def _default_user_state_db_candidates() -> set[Path]:
    home = Path.home().expanduser().resolve()
    return {
        (home / paths.AVIBE_HOME_DIRNAME / "state" / "vibe.sqlite").resolve(),
        (home / paths.LEGACY_HOME_DIRNAME / "state" / "vibe.sqlite").resolve(),
    }


def _env_flag_enabled(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _reset_unreleased_initial_schema_drift(db_path: Path) -> None:
    path = db_path.expanduser().resolve()
    if not path.exists():
        return

    with sqlite3.connect(path) as conn:
        tables = _table_names(conn)
        if INITIAL_TABLES.issubset(tables):
            return
        if "alembic_version" not in tables:
            return
        version = conn.execute("select version_num from alembic_version").fetchone()
        if version != (INITIAL_REVISION,):
            return
        if not any(table in tables for table in UNRELEASED_ONLY_INITIAL_TABLES):
            return

        conn.execute("PRAGMA foreign_keys = OFF")
        for table in UNRELEASED_OLD_INITIAL_TABLES:
            if table in tables:
                conn.execute(f'drop table if exists "{table}"')
        conn.commit()


def _repair_unreleased_head_schema_drift(db_path: Path) -> None:
    path = db_path.expanduser().resolve()
    if not path.exists():
        return

    with sqlite3.connect(path) as conn:
        tables = _table_names(conn)
        _rename_legacy_background_tables(conn, tables)
        tables = _table_names(conn)
        if not (tables <= {"alembic_version", "agents"}):
            _ensure_agents_table(conn, tables)
            tables = _table_names(conn)
        _repair_initial_required_columns(conn, tables)
        tables = _table_names(conn)
        if not PRE_SHOW_SESSION_EVENTS_HEAD_TABLES.issubset(tables):
            return
        if "alembic_version" not in tables:
            return
        version = conn.execute("select version_num from alembic_version").fetchone()
        if version not in {("20260515_0002",), ("20260522_0003",), ("20260523_0004",)}:
            return

        if not _pre_show_session_events_head_schema_ready(conn, tables) and _repair_head_required_columns(conn, tables):
            conn.commit()


def _stamp_existing_initial_schema(db_path: Path, cfg: Config) -> None:
    path = db_path.expanduser().resolve()
    if not path.exists():
        return

    with sqlite3.connect(path) as conn:
        tables = _table_names(conn)
        _rename_legacy_background_tables(conn, tables)
        tables = _table_names(conn)
        if not (tables <= {"alembic_version"}):
            _ensure_agents_table(conn, tables)
            tables = _table_names(conn)
        _repair_initial_required_columns(conn, tables)
        tables = _table_names(conn)
        if not tables:
            return
        if not (tables - {"alembic_version", "agents"}):
            return
        if "alembic_version" in tables:
            version = conn.execute("select version_num from alembic_version").fetchone()
            if version is not None and version[0]:
                return
        missing_initial_tables = INITIAL_TABLES - tables
        if not (tables & (INITIAL_TABLES - {"agents"})) and (tables & {"run_definitions", "agent_runs"}):
            return
        if missing_initial_tables and (tables & INITIAL_TABLES):
            missing = ", ".join(sorted(missing_initial_tables))
            raise RuntimeError(f"existing SQLite schema is incomplete; missing initial tables: {missing}")
        if not INITIAL_TABLES.issubset(tables):
            return
        if HEAD_TABLES.issubset(tables):
            if not _head_schema_ready(conn, tables):
                _repair_head_required_columns(conn, tables)
                conn.commit()
                tables = _table_names(conn)
            if not _head_schema_ready(conn, tables):
                missing = _missing_head_schema_description(conn, tables)
                raise RuntimeError(f"existing SQLite head schema is incomplete; missing: {missing}")
            _ensure_head_indexes(conn, tables)
            _delete_historical_message_tool_calls(conn, tables)
            conn.commit()
            _run_remove_legacy_default_agent_migration(db_path)
            command.stamp(cfg, LATEST_SCHEMA_REVISION)
            _run_post_stamp_data_migrations(db_path)
            return
        if PRE_SHOW_SESSION_EVENTS_HEAD_TABLES.issubset(tables):
            if not _pre_show_session_events_head_schema_ready(conn, tables):
                _repair_head_required_columns(conn, tables)
                conn.commit()
                tables = _table_names(conn)
            if not _pre_show_session_events_head_schema_ready(conn, tables):
                missing = _missing_pre_show_session_events_head_schema_description(conn, tables)
                raise RuntimeError(f"existing SQLite head schema is incomplete; missing: {missing}")
            _run_remove_legacy_default_agent_migration(db_path)
            command.stamp(cfg, REMOVE_LEGACY_DEFAULT_AGENT_REVISION)
            _run_post_stamp_data_migrations(db_path)
            return

    command.stamp(cfg, INITIAL_REVISION)


def _run_remove_legacy_default_agent_migration(db_path: Path) -> None:
    revision_module = import_module("storage.alembic.versions.20260530_0008_remove_legacy_default_agent")
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            original_op = revision_module.op
            revision_module.op = SimpleNamespace(get_bind=lambda: conn)
            try:
                revision_module.upgrade()
            finally:
                revision_module.op = original_op
    finally:
        engine.dispose()


def _run_post_stamp_data_migrations(db_path: Path) -> None:
    from storage.importer import _run_sqlite_data_migrations

    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            _run_sqlite_data_migrations(conn)
    finally:
        engine.dispose()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type = 'table'",
        )
    }


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'pragma table_info("{table}")')}


def _head_schema_ready(conn: sqlite3.Connection, tables: set[str]) -> bool:
    if not HEAD_TABLES.issubset(tables):
        return False
    required = HEAD_REQUIRED_COLUMNS | HEAD_ONLY_REQUIRED_COLUMNS
    return all(required_columns.issubset(_column_names(conn, table)) for table, required_columns in required.items())


def _pre_show_session_events_head_schema_ready(conn: sqlite3.Connection, tables: set[str]) -> bool:
    if not PRE_SHOW_SESSION_EVENTS_HEAD_TABLES.issubset(tables):
        return False
    return all(
        required_columns.issubset(_column_names(conn, table))
        for table, required_columns in PRE_SHOW_SESSION_EVENTS_REQUIRED_COLUMNS.items()
    )


def _repair_head_required_columns(conn: sqlite3.Connection, tables: set[str]) -> bool:
    if not PRE_SHOW_SESSION_EVENTS_HEAD_TABLES.issubset(tables):
        return False
    changed = False
    changed = _repair_initial_required_columns(conn, tables) or changed

    definition_columns = _column_names(conn, "run_definitions")
    if "definition_type" not in definition_columns and "task_type" in definition_columns:
        conn.execute('alter table "run_definitions" rename column "task_type" to "definition_type"')
        changed = True
        definition_columns = _column_names(conn, "run_definitions")
    for column, column_type in {
        "deleted_at": "VARCHAR",
        "agent_name": "VARCHAR",
        "session_policy": "VARCHAR",
        "message": "TEXT",
        "message_payload_json": "TEXT",
        "last_run_id": "VARCHAR",
        "retired_at": "VARCHAR",
        "retirement_reason": "VARCHAR",
    }.items():
        if column not in definition_columns:
            conn.execute(f'alter table "run_definitions" add column "{column}" {column_type}')
            changed = True
    definition_columns = _column_names(conn, "run_definitions")
    if "message" in definition_columns and "prompt" in definition_columns:
        conn.execute('update "run_definitions" set message = prompt where message is null')
    if "session_policy" in definition_columns:
        conn.execute(
            'update "run_definitions" set session_policy = '
            'case '
            'when session_id is not null and session_id != "" then "existing" '
            'when legacy_session_key is not null and legacy_session_key != "" then "existing" '
            "else null end "
            'where session_policy is null'
        )

    run_columns = _column_names(conn, "agent_runs")
    if "definition_id" not in run_columns and "task_id" in run_columns:
        conn.execute('alter table "agent_runs" rename column "task_id" to "definition_id"')
        changed = True
        run_columns = _column_names(conn, "agent_runs")
    for column, column_type in {
        "source_kind": "VARCHAR",
        "source_actor": "TEXT",
        "parent_run_id": "VARCHAR",
        "agent_name": "VARCHAR",
        "agent_id": "VARCHAR",
        "agent_backend": "VARCHAR",
        "model": "VARCHAR",
        "reasoning_effort": "VARCHAR",
        "session_policy": "VARCHAR",
        "message": "TEXT",
        "message_payload_json": "TEXT",
        "result_text": "TEXT",
        "result_payload_json": "TEXT",
        "message_ids_json": "TEXT",
        "callback_session_id": "VARCHAR",
        "callback_status": "VARCHAR",
        "callback_error": "TEXT",
        "callback_run_id": "VARCHAR",
        "callback_completed_at": "VARCHAR",
        "callback_terminal_turn_id": "VARCHAR",
        "cancel_requested": "INTEGER not null default 0",
        "cancel_requested_at": "VARCHAR",
    }.items():
        if column not in run_columns:
            conn.execute(f'alter table "agent_runs" add column "{column}" {column_type}')
            changed = True
    run_columns = _column_names(conn, "agent_runs")
    if "message" in run_columns and "prompt" in run_columns:
        conn.execute('update "agent_runs" set message = prompt where message is null')

    # messages.type (20260531_0009): add + backfill, mirroring the migration, so
    # a drifted/unversioned head schema reaches readiness instead of leaving
    # messages_service.append writing a column that doesn't exist.
    if "messages" in tables and "type" not in _column_names(conn, "messages"):
        conn.execute('alter table "messages" add column "type" VARCHAR not null default \'assistant\'')
        conn.execute(
            """
            update messages set type = case
                when author = 'user' then 'user'
                when json_extract(content_json, '$.kind') = 'notify' then 'notify'
                when json_extract(content_json, '$.kind') = 'result' then 'result'
                when json_extract(content_json, '$.kind') in ('toolcall', 'tool_call') then 'tool_call'
                else 'assistant'
            end
            """
        )
        changed = True

    # messages.source (20260531_0010): origin (user/agent/harness), distinct
    # from author. Mirror the migration's add + backfill for drifted heads.
    if "messages" in tables and "source" not in _column_names(conn, "messages"):
        conn.execute('alter table "messages" add column "source" VARCHAR')
        conn.execute(
            """
            update messages set source = case
                when author = 'user' then 'user'
                when author = 'agent' then 'agent'
                when author = 'system' then 'agent'
                else null
            end
            """
        )
        changed = True

    if "web_push_subscriptions" in tables and "device_id" not in _column_names(conn, "web_push_subscriptions"):
        conn.execute('alter table "web_push_subscriptions" add column "device_id" VARCHAR')
        changed = True
    if "web_push_subscriptions" in tables and "provider_invalidated_at" not in _column_names(conn, "web_push_subscriptions"):
        conn.execute('alter table "web_push_subscriptions" add column "provider_invalidated_at" VARCHAR')
        changed = True
    # context_seq (20261002_0063): the Avibe Agent's model-context order key.
    for table in ("messages", "agent_events"):
        if table in tables and "context_seq" not in _column_names(conn, table):
            conn.execute(f'alter table "{table}" add column "context_seq" INTEGER')
            changed = True
    if "vault_secrets" in tables and "fields" not in _column_names(conn, "vault_secrets"):
        conn.execute('alter table "vault_secrets" add column "fields" TEXT')
        changed = True

    _ensure_head_indexes(conn, tables)
    return changed


def _rename_legacy_background_tables(conn: sqlite3.Connection, tables: set[str]) -> bool:
    changed = False
    if "background_tasks" in tables and "run_definitions" not in tables:
        conn.execute('alter table "background_tasks" rename to "run_definitions"')
        changed = True
    if "background_runs" in tables and "agent_runs" not in tables:
        conn.execute('alter table "background_runs" rename to "agent_runs"')
        changed = True
    return changed


def _ensure_agents_table(conn: sqlite3.Connection, tables: set[str]) -> bool:
    if "agents" in tables:
        return False
    conn.execute(
        """
        create table agents (
            id varchar primary key,
            name varchar not null,
            normalized_name varchar not null,
            description text,
            backend varchar not null,
            model varchar,
            reasoning_effort varchar,
            system_prompt text,
            enabled integer not null default 1,
            source varchar not null,
            source_ref text,
            metadata_json text not null,
            archived_at varchar,
            created_at varchar not null,
            updated_at varchar not null,
            constraint uq_agents_normalized_name unique (normalized_name)
        )
        """
    )
    conn.execute('create index if not exists ix_agents_backend on agents (backend)')
    conn.execute('create index if not exists ix_agents_updated on agents (updated_at)')
    return True


def _repair_initial_required_columns(conn: sqlite3.Connection, tables: set[str]) -> bool:
    changed = False
    if "agents" in tables:
        columns = _column_names(conn, "agents")
        if "enabled" not in columns:
            conn.execute('alter table "agents" add column "enabled" INTEGER not null default 1')
            changed = True
        if "archived_at" not in columns:
            conn.execute('alter table "agents" add column "archived_at" VARCHAR')
            changed = True
    if "scope_settings" in tables:
        columns = _column_names(conn, "scope_settings")
        if "agent_name" not in columns:
            conn.execute('alter table "scope_settings" add column "agent_name" VARCHAR')
            changed = True
    if "agent_sessions" in tables:
        columns = _column_names(conn, "agent_sessions")
        if "agent_id" not in columns:
            conn.execute('alter table "agent_sessions" add column "agent_id" VARCHAR')
            changed = True
        if "agent_name" not in columns:
            conn.execute('alter table "agent_sessions" add column "agent_name" VARCHAR')
            changed = True
        if "visibility" not in columns:
            conn.execute(
                'alter table "agent_sessions" add column "visibility" '
                "VARCHAR not null default 'foreground'"
            )
            changed = True
        if "pinned" not in columns:
            conn.execute(
                'alter table "agent_sessions" add column "pinned" '
                "INTEGER not null default 0"
            )
            changed = True
    return changed


def _ensure_new_background_indexes(conn: sqlite3.Connection) -> None:
    conn.execute('create index if not exists ix_run_definitions_type_enabled on run_definitions (definition_type, enabled)')
    conn.execute('create index if not exists ix_run_definitions_session on run_definitions (session_id)')
    conn.execute('create index if not exists ix_run_definitions_agent on run_definitions (agent_name)')
    conn.execute('create index if not exists ix_run_definitions_updated on run_definitions (updated_at)')
    conn.execute('create index if not exists ix_agent_runs_definition_created on agent_runs (definition_id, created_at)')
    conn.execute('create index if not exists ix_agent_runs_definition_status on agent_runs (definition_id, status)')
    conn.execute('create index if not exists ix_agent_runs_status_created on agent_runs (status, created_at)')
    conn.execute('create index if not exists ix_agent_runs_type_status_created on agent_runs (run_type, status, created_at)')
    conn.execute('create index if not exists ix_agent_runs_session_created on agent_runs (session_id, created_at)')
    conn.execute('create index if not exists ix_agent_runs_callback_session on agent_runs (callback_session_id)')
    conn.execute('create index if not exists ix_agent_runs_source_actor on agent_runs (source_kind, source_actor)')
    conn.execute('create index if not exists ix_agent_runs_source_session on agent_runs (source_kind, session_id, source_actor)')
    conn.execute('create index if not exists ix_agent_runs_agent_created on agent_runs (agent_name, created_at)')
    conn.execute('create index if not exists ix_agent_runs_callback_status on agent_runs (callback_status, completed_at)')
    conn.execute('create index if not exists ix_agent_runs_updated on agent_runs (updated_at)')
    live_index = import_module("storage.alembic.versions.20261018_0070_run_definitions_live_index")
    live_row = conn.execute(
        "select sql from sqlite_master where type = 'index' and name = ?",
        (live_index._INDEX,),
    ).fetchone()
    if live_row is None or _normalized_index_sql(live_row[0]) != _normalized_index_sql(live_index.CREATE_INDEX_SQL):
        conn.execute(live_index.DROP_INDEX_SQL)
        conn.execute(live_index.CREATE_INDEX_SQL)
    resident_index = import_module("storage.alembic.versions.20261107_0074_resident_definitions_index")
    conn.execute(resident_index.CREATE_INDEX_SQL)
    if not _agent_runs_managed_indexes_ready(conn):
        _ensure_agent_runs_managed_indexes(conn)


#: The ``agent_runs`` index migrations whose DDL the head-schema repair path must also
#: install, oldest first. 0040 is deliberately absent: 0041 drops the index 0040 created
#: and recreates it under its own two-expression definition, so 0040's shape is never a
#: valid end state and replaying it here would only build an index 0041 has to replace.
_AGENT_RUNS_INDEX_REVISION_MODULES = (
    "storage.alembic.versions.20260728_0039_agent_runs_settled_at_index",
    "storage.alembic.versions.20260728_0041_agent_runs_owed_notice_backoff_index",
    "storage.alembic.versions.20260729_0042_agent_runs_definition_streak_index",
    "storage.alembic.versions.20260811_0051_callback_terminal_turn_identity",
    "storage.alembic.versions.20261015_0066_agent_runs_callback_parent_index",
)
#: Columns the managed indexes read. A head-shaped database has all of them, but
#: this helper is also reached from ``_repair_head_required_columns`` on older drifted
#: shapes, and ``create index`` on a missing column raises rather than skipping.
_AGENT_RUNS_INDEX_COLUMNS = frozenset(
    {
        "definition_id",
        "created_at",
        "completed_at",
        "metadata_json",
        "callback_terminal_turn_id",
        "session_id",
        "run_type",
        "source_kind",
        "parent_run_id",
        "source_actor",
        "id",
    }
)


def _normalized_index_sql(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _agent_runs_managed_indexes_ready(conn: sqlite3.Connection) -> bool:
    if not _AGENT_RUNS_INDEX_COLUMNS.issubset(_column_names(conn, "agent_runs")):
        return True
    for module_name in _AGENT_RUNS_INDEX_REVISION_MODULES:
        revision_module = import_module(module_name)
        row = conn.execute(
            "select sql from sqlite_master where type = 'index' and name = ?",
            (revision_module._INDEX,),
        ).fetchone()
        if row is None or _normalized_index_sql(row[0]) != _normalized_index_sql(revision_module.CREATE_INDEX_SQL):
            return False
    return True


def _ensure_agent_runs_managed_indexes(conn: sqlite3.Connection) -> None:
    """Install migration-owned ``agent_runs`` indexes on a head-shaped database.

    ``_ensure_head_indexes`` promises that a head-shaped unversioned database ends up
    with every index head has. It once silently lagged the three performance indexes;
    the same repair path now owns the callback identity index too.
    Two lanes make that gap reachable without any migration ever running: a database
    born from ``metadata.create_all`` satisfies ``background_tables_ready`` (tables and
    columns only — never indexes), so ``SQLiteBackgroundTaskStore`` accepts it as ready
    and never calls ``initialize_background_tables``; and the day
    ``LATEST_SCHEMA_REVISION`` moves past 0038 the stamp floor stops replaying them for
    everyone. Both leave the two-second owed-notice tick, the health window and the
    streak read scanning run history with nothing to repair them.

    The DDL is IMPORTED from the revision that owns it, never retyped. The owed-notice
    index is an expression index and SQLite matches an index expression against a query
    expression by text, so a second copy of that string is an index that gets built and
    silently ignored — the failure 0040 and 0041 were each written to correct. Importing
    the revision module is the same technique ``_run_remove_legacy_default_agent_migration``
    already uses; ``storage.background`` (which holds the query-side twins) cannot be
    imported here because it imports this module.

    Drop-then-create rather than ``create index if not exists``: a database carrying the
    superseded 0040 shape of ``ix_agent_runs_owed_notice`` has the NAME already taken by
    an index missing the backoff expression, and ``if not exists`` would leave that
    weaker definition in place. Recreating unconditionally also makes the resulting
    ``sqlite_master`` SQL byte-identical to the migration's, whichever route a database
    took to head.
    """

    if not _AGENT_RUNS_INDEX_COLUMNS.issubset(_column_names(conn, "agent_runs")):
        return
    for module_name in _AGENT_RUNS_INDEX_REVISION_MODULES:
        revision_module = import_module(module_name)
        conn.execute(revision_module.DROP_INDEX_SQL)
        conn.execute(revision_module.CREATE_INDEX_SQL)


def _ensure_messages_query_indexes(conn: sqlite3.Connection, tables: set[str]) -> None:
    if "agent_sessions" in tables:
        conn.execute(
            "create index if not exists ix_agent_sessions_visibility "
            "on agent_sessions (visibility)"
        )
        conn.execute(
            "create index if not exists ix_agent_sessions_status_visibility_activity "
            "on agent_sessions (status, visibility, last_active_at, created_at, id)"
        )
        conn.execute(
            "create index if not exists ix_agent_sessions_scope_status_visibility_activity "
            "on agent_sessions (scope_id, status, visibility, last_active_at, created_at, id)"
        )
        conn.execute(
            "create index if not exists ix_agent_sessions_scope_status_activity "
            "on agent_sessions (scope_id, status, last_active_at, created_at, id)"
        )
        conn.execute(
            "create index if not exists ix_agent_sessions_scope_status_pinned_activity "
            "on agent_sessions (scope_id, status, pinned, last_active_at, created_at, id)"
        )
        status_index = import_module(
            "storage.alembic.versions.20261016_0068_agent_sessions_agent_status_index"
        )
        status_row = conn.execute(
            "select sql from sqlite_master where type = 'index' and name = ?",
            (status_index._INDEX,),
        ).fetchone()
        if status_row is None or _normalized_index_sql(status_row[0]) != _normalized_index_sql(
            status_index.CREATE_INDEX_SQL
        ):
            conn.execute(status_index.DROP_INDEX_SQL)
            conn.execute(status_index.CREATE_INDEX_SQL)
    if "messages" not in tables:
        return
    transcript_index = import_module(
        "storage.alembic.versions.20260804_0046_message_transcript_order"
    )
    transcript_row = conn.execute(
        "select sql from sqlite_master where type = 'index' and name = ?",
        (transcript_index._INDEX,),
    ).fetchone()
    if (
        transcript_row is None
        or _normalized_index_sql(transcript_row[0])
        != _normalized_index_sql(transcript_index.CREATE_INDEX_SQL)
    ):
        conn.execute(transcript_index.DROP_INDEX_SQL)
        conn.execute(transcript_index.CREATE_INDEX_SQL)
    # Read expression/partial-index DDL from its owning revision: SQLite matches
    # these predicates syntactically, so a retyped copy may be silently ignored.
    turn_index = import_module("storage.alembic.versions.20261012_0064_messages_turn_id_index")
    recovery_index = import_module("storage.alembic.versions.20261016_0067_vibey_recovery_input_index")
    indexes = [
        (turn_index._INDEX, turn_index.DROP_INDEX_SQL, turn_index.CREATE_INDEX_SQL),
        (recovery_index._INDEX, recovery_index.DROP_INDEX_SQL, recovery_index.CREATE_INDEX_SQL),
    ]
    if "message_deliveries" in tables:
        indexes.append((
            recovery_index._DELIVERY_INDEX,
            recovery_index.DROP_DELIVERY_INDEX_SQL,
            recovery_index.CREATE_DELIVERY_INDEX_SQL,
        ))
    for index_name, drop_sql, create_sql in indexes:
        row = conn.execute(
            "select sql from sqlite_master where type = 'index' and name = ?", (index_name,)
        ).fetchone()
        if row is None or _normalized_index_sql(row[0]) != _normalized_index_sql(create_sql):
            conn.execute(drop_sql)
            conn.execute(create_sql)
    conn.execute('create index if not exists ix_messages_session_created_id on messages (session_id, created_at, id)')
    conn.execute('create index if not exists ix_messages_session_type_created_id on messages (session_id, type, created_at, id)')
    conn.execute('create index if not exists ix_messages_platform_session_created_id on messages (platform, session_id, created_at, id)')
    conn.execute(
        'create index if not exists ix_messages_unread_session '
        'on messages (platform, type, author, read_at, session_id)'
    )
    for index_name, index_sql in transcript_index._INDEX_SQL.items():
        if index_name == transcript_index._INDEX:
            continue
        row = conn.execute(
            "select sql from sqlite_master where type = 'index' and name = ?",
            (index_name,),
        ).fetchone()
        if row is not None and _normalized_index_sql(row[0]) == _normalized_index_sql(
            index_sql
        ):
            continue
        conn.execute(f"drop index if exists {index_name}")
        conn.execute(index_sql)


def _ensure_agent_events_indexes(conn: sqlite3.Connection, tables: set[str]) -> None:
    if "agent_events" not in tables:
        return
    conn.execute(
        "create index if not exists ix_agent_events_session_created_id "
        "on agent_events (session_id, created_at, id)"
    )
    conn.execute(
        "create index if not exists ix_agent_events_session_type_created_id "
        "on agent_events (session_id, event_type, created_at, id)"
    )
    conn.execute(
        "create index if not exists ix_agent_events_scope_created_id "
        "on agent_events (scope_id, created_at, id)"
    )
    conn.execute(
        "create index if not exists ix_agent_events_turn_sequence_id "
        "on agent_events (turn_id, sequence, id)"
    )
    # The model-context order key of both tables (20261002_0063).
    for table in ("messages", "agent_events"):
        conn.execute(
            f"create unique index if not exists uq_{table}_session_context_seq "
            f"on {table} (session_id, context_seq) where context_seq is not null"
        )


def _ensure_vault_authz_indexes(conn: sqlite3.Connection, tables: set[str]) -> None:
    if "vault_auth_factors" in tables:
        conn.execute(
            "create index if not exists ix_vault_auth_factors_kind_rp "
            "on vault_auth_factors (kind, rp_id, disabled_at)"
        )
    if "vault_operation_challenges" in tables:
        conn.execute(
            "create index if not exists ix_vault_operation_challenges_lookup "
            "on vault_operation_challenges (operation, secret_name, expires_at)"
        )
        conn.execute(
            "create index if not exists ix_vault_operation_challenges_consumed "
            "on vault_operation_challenges (consumed_at, expires_at)"
        )


def _ensure_resource_access_indexes(conn: sqlite3.Connection, tables: set[str]) -> None:
    if "resource_access_policies" in tables:
        conn.execute(
            "create index if not exists ix_resource_access_policies_org_level "
            "on resource_access_policies (organization_id, access_level, resource_kind)"
        )
        conn.execute(
            "create index if not exists ix_resource_access_policies_owner "
            "on resource_access_policies (owner_user_id, resource_kind)"
        )
    if "resource_access_groups" in tables:
        conn.execute(
            "create index if not exists ix_resource_access_groups_group "
            "on resource_access_groups (organization_id, group_id, resource_kind)"
        )


def _delete_historical_message_tool_calls(conn: sqlite3.Connection, tables: set[str]) -> None:
    if "messages" not in tables:
        return
    if "show_session_events" in tables:
        conn.execute(
            """
            update show_session_events
            set message_id = null
            where message_id in (select id from messages where type = 'tool_call')
            """
        )
    if "media_objects" in tables:
        conn.execute(
            """
            update media_objects
            set message_id = null
            where message_id in (select id from messages where type = 'tool_call')
            """
        )
    conn.execute("delete from messages where type = 'tool_call'")


def _ensure_head_indexes(conn: sqlite3.Connection, tables: set[str]) -> None:
    if {"run_definitions", "agent_runs"}.issubset(tables):
        _ensure_new_background_indexes(conn)
    _ensure_messages_query_indexes(conn, tables)
    _ensure_agent_events_indexes(conn, tables)
    _ensure_vault_authz_indexes(conn, tables)
    _ensure_resource_access_indexes(conn, tables)


def _missing_head_schema_description(conn: sqlite3.Connection, tables: set[str]) -> str:
    missing_parts = [f"tables {', '.join(sorted(HEAD_TABLES - tables))}"] if not HEAD_TABLES.issubset(tables) else []
    for table, required_columns in (HEAD_REQUIRED_COLUMNS | HEAD_ONLY_REQUIRED_COLUMNS).items():
        if table not in tables:
            continue
        missing_columns = required_columns - _column_names(conn, table)
        if missing_columns:
            missing_parts.append(f"{table}.{', '.join(sorted(missing_columns))}")
    return "; ".join(missing_parts) or "unknown head schema drift"


def _missing_pre_show_session_events_head_schema_description(conn: sqlite3.Connection, tables: set[str]) -> str:
    missing_parts = (
        [f"tables {', '.join(sorted(PRE_SHOW_SESSION_EVENTS_HEAD_TABLES - tables))}"]
        if not PRE_SHOW_SESSION_EVENTS_HEAD_TABLES.issubset(tables)
        else []
    )
    for table, required_columns in PRE_SHOW_SESSION_EVENTS_REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        missing_columns = required_columns - _column_names(conn, table)
        if missing_columns:
            missing_parts.append(f"{table}.{', '.join(sorted(missing_columns))}")
    return "; ".join(missing_parts) or "unknown head schema drift"
