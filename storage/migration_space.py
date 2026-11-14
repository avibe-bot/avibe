"""Read-only capacity admission before rollback copying or migration writes.

This is a conservative free-space policy, not a reservation or a measured
minimum. Other disk users, quotas and later I/O failures can still exhaust space.
Never credit clone success, pruning, sparse allocation or an older backup.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import importlib
import math
import os
from pathlib import Path
import shutil
import sqlite3

_MIB = 1024 * 1024
_BOUNDARY_MODULE = "storage.alembic.versions.20261114_0089_coordinated_writers"


class MigrationSpaceCheckError(RuntimeError):
    """Capacity could not be established; no copy/migration was admitted."""


class InsufficientMigrationSpaceError(MigrationSpaceCheckError):
    def __init__(self, plan: SpacePlan, *, recovered: bool = False):
        self.plan = plan
        lines = ["SQLite upgrade not started: insufficient free space for a fresh rollback copy and migration workspace."]
        if recovered:
            lines.insert(0, "SQLite crash recovery already completed; database/journal bytes may have changed.")
        for volume in plan.volumes:
            if volume.available_bytes < plan.required_bytes:
                deficit = plan.required_bytes - volume.available_bytes
                lines.append(
                    f"{', '.join(volume.paths)}: available {_gb(volume.available_bytes)} GB, "
                    f"required reserve {_gb(plan.required_bytes)} GB ({plan.required_bytes} bytes). "
                    f"Free at least {_gb(deficit)} GB more here and retry."
                )
        lines.append(
            "This is a conservative reserve, not the measured minimum. "
            "No database/WAL checkpoint, migration, backup creation or pruning was performed by this admission check."
        )
        super().__init__("\n".join(lines))


def _gb(value: int) -> str:
    # Round UP, so the human-readable deficit never understates the bytes needed.
    return f"{math.ceil(value / 10_000_000) / 100:.2f}"


@dataclass(frozen=True)
class Capacity:
    paths: tuple[str, ...]
    available_bytes: int


@dataclass(frozen=True)
class SpacePlan:
    backup_bytes: int
    database_workspace_bytes: int
    temporary_bytes: int
    margin_bytes: int
    required_bytes: int
    catalog_reference_rows: int | None
    volumes: tuple[Capacity, ...]

    def require(self, *, recovered: bool = False) -> SpacePlan:
        if any(volume.available_bytes < self.required_bytes for volume in self.volumes):
            raise InsufficientMigrationSpaceError(self, recovered=recovered)
        return self

    def to_dict(self) -> dict:
        return asdict(self)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _existing_directory(path: Path) -> Path:
    path = path.expanduser().resolve()
    while not path.exists():
        if path.parent == path:
            raise OSError(f"no existing filesystem for {path}")
        path = path.parent
    if not path.is_dir():
        path = path.parent
    return path


def _sqlite_temp_directory(conn: sqlite3.Connection) -> Path:
    configured = conn.execute("PRAGMA temp_store_directory").fetchone()
    candidates = [Path(configured[0])] if configured and configured[0] else []
    if os.name == "nt":
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetTempPathW(len(buffer), buffer)
        if not 0 < length < len(buffer):
            raise OSError("Windows SQLite temporary directory could not be determined")
        candidates.append(Path(buffer.value))
    else:
        # SQLite's stock Unix VFS order, not Python tempfile's different order.
        candidates += [Path(value) for value in (os.environ.get("SQLITE_TMPDIR"), os.environ.get("TMPDIR")) if value]
        candidates += [Path("/var/tmp"), Path("/usr/tmp"), Path("/tmp"), Path.cwd()]
    for candidate in candidates:
        if candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK):
            return candidate.resolve()
    raise OSError("SQLite temporary directory is unavailable; cannot verify migration capacity")


def plan_space(
    db_path: Path, *, backups_dir: Path | None = None, migration: bool = False, coordinated: bool = False,
) -> SpacePlan:
    """Inspect existing bytes/capacity without mkdir, checkpoint or RW connection.

    The exact 3.2.6→epoch-3 transition sizes its fixed-key partial index from a
    read-only Skill-index scan. Other migration paths use a deliberately looser
    two-database workspace policy. Distinct volumes may share APFS/Btrfs pools:
    compare the all-role total against each capacity reading, rather than assume
    independent capacity. This can over-refuse but never double-spends a pool.
    """
    try:
        source = db_path.expanduser().resolve()
        main = source.stat().st_size
        journals = _file_size(source.with_name(source.name + "-wal")) + _file_size(source.with_name(source.name + "-journal"))
        backup = main + journals
        database_workspace, temporary = journals, 0
        rows = None
        temporary_dir = None
        if migration:
            # The generic policy needs only filesystem sizes and the process's
            # SQLite temp directory, so it also works before hot-journal recovery.
            database = f"{source.as_uri()}?mode=ro" if coordinated else ":memory:"
            with closing(sqlite3.connect(database, uri=coordinated)) as conn:
                conn.execute("BEGIN")
                if coordinated:
                    boundary = importlib.import_module(_BOUNDARY_MODULE)
                    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                    rows = conn.execute(
                        "SELECT count(*) FROM agent_events INDEXED BY ix_agent_events_skill_created_id "
                        "WHERE visibility = 'trace' AND event_type IN ('skill.catalog_result', 'skill.load_result') "
                        f"AND ({boundary._PREDICATE})"
                    ).fetchone()[0]
                    # Very loose per-entry page policy for fixed 64-hex key +
                    # rowid, small/UTF-16 pages, roots and pointer-map overhead.
                    index_bytes = 3 * (rows + 16) * page_size
                    wal_bytes = 32 + math.ceil(index_bytes / page_size) * (page_size + 24)
                    database_workspace += index_bytes + wal_bytes
                    temporary = 2 * index_bytes if rows else 0
                else:
                    database_workspace += 2 * backup
                    temporary = 2 * backup
                if temporary:
                    temporary_dir = _sqlite_temp_directory(conn)
        roles = [(source.parent, database_workspace), ((backups_dir or source.parent / "backups"), backup)]
        if temporary_dir is not None:
            roles.append((temporary_dir, temporary))
        total = sum(amount for _, amount in roles)
        margin = max(256 * _MIB, math.ceil(total / 10))
        pools: dict[int, tuple[list[str], int]] = {}
        for path, _ in roles:
            directory = _existing_directory(path)
            device = directory.stat().st_dev
            free = shutil.disk_usage(directory).free
            if type(free) is not int or free < 0:
                raise OSError(f"invalid available-space reading for {directory}")
            label = str(path.expanduser().resolve())
            labels, minimum = pools.get(device, ([], free))
            if label not in labels:
                labels.append(label)
            pools[device] = (labels, min(minimum, free))
        return SpacePlan(backup, database_workspace, temporary, margin, total + margin, rows,
                         tuple(Capacity(tuple(labels), free) for labels, free in pools.values()))
    except (OSError, sqlite3.Error) as exc:
        raise MigrationSpaceCheckError(
            f"SQLite upgrade capacity could not be verified before copying or migration: {exc}. "
            "Check filesystem access and free space, then retry."
        ) from exc
