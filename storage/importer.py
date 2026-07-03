from __future__ import annotations

import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, func, select

from config import paths
from config.discovered_chats import DiscoveredChatsStore
from config.v2_sessions import (
    SessionState,
    infer_platform_from_thread_ids,
    load_session_state_from_json,
    migrate_session_state_active_polls,
    migrate_session_state_mappings,
)
from config.v2_settings import SettingsState, load_settings_state_from_json
from storage.db import create_sqlite_engine
from storage.lock import MigrationFileLock
from storage.migrations import guard_source_checkout_default_state_migration, run_migrations
from storage.models import (
    agents,
    agent_sessions,
    agent_runs,
    auth_codes,
    imported_state_tables,
    run_definitions,
    runtime_records,
    scope_settings,
    scopes,
    state_meta,
)
from storage.sessions_service import SESSIONS_LAST_ACTIVITY_KEY, SQLiteSessionsService
from storage.settings_service import SQLiteSettingsService, upsert_scope

JSON_IMPORT_MARKER = "json_import_completed_at"
BACKGROUND_IMPORT_MARKER = "background_json_import_completed_at"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrationImportReport:
    db_path: Path
    imported: bool
    backup_path: Path | None = None
    counts: dict[str, int] = field(default_factory=dict)


def ensure_sqlite_state(
    *,
    db_path: Path | None = None,
    state_dir: Path | None = None,
    primary_platform: str | None = None,
) -> MigrationImportReport:
    """Create/migrate the SQLite DB and import existing JSON state once."""

    target_db = (db_path or paths.get_sqlite_state_path()).expanduser().resolve()
    target_state_dir = (state_dir or paths.get_state_dir()).expanduser().resolve()
    guard_source_checkout_default_state_migration(target_db)
    _ensure_sqlite_target_dirs(
        target_state_dir=target_state_dir,
        target_db=target_db,
        use_default_dirs=db_path is None and state_dir is None,
    )
    lock_path = target_state_dir / "migration.lock"

    with MigrationFileLock(lock_path):
        run_migrations(target_db)
        engine = create_sqlite_engine(target_db)
        try:
            backup_path: Path | None = None
            with engine.begin() as conn:
                if _has_import_marker(conn):
                    imported_background = False
                    background_counts: dict[str, int] = {}
                    if not _has_background_import_marker(conn):
                        backup_path = _backup_json_state(target_state_dir)
                        background_counts = _import_background_state(conn, target_state_dir)
                        _set_background_import_marker(conn)
                        _validate_import(conn, _current_counts(conn))
                        imported_background = bool(
                            background_counts.get("background_scheduled_tasks")
                            or background_counts.get("background_watches")
                            or background_counts.get("background_runs_imported")
                        )
                    data_migration_counts = _run_sqlite_data_migrations(conn)
                    return MigrationImportReport(
                        db_path=target_db,
                        imported=imported_background,
                        backup_path=backup_path,
                        counts=_current_counts(conn) | background_counts | data_migration_counts,
                    )

                _clear_imported_state(conn)

            backup_path = _backup_json_state(target_state_dir)
            parsed = _parse_json_state(target_state_dir, primary_platform=primary_platform)
            _write_parsed_state(target_db, parsed)

            with engine.begin() as conn:
                discovered_count = _import_discovered_chats(conn, parsed.discovered)
                background_counts = _import_background_state(conn, target_state_dir)
                data_migration_counts = _run_sqlite_data_migrations(conn)
                counts = _current_counts(conn)
                counts["discovered_scopes"] = discovered_count
                counts.update(background_counts)
                counts.update(data_migration_counts)
                if parsed.discovered_skipped:
                    counts["discovered_chats_skipped"] = 1
                _validate_import(conn, counts)
                _set_import_marker(conn)
                _set_background_import_marker(conn)
                return MigrationImportReport(
                    db_path=target_db,
                    imported=True,
                    backup_path=backup_path,
                    counts=_current_counts(conn)
                    | {"discovered_scopes": discovered_count}
                    | background_counts
                    | data_migration_counts
                    | ({"discovered_chats_skipped": 1} if parsed.discovered_skipped else {}),
                )
        finally:
            engine.dispose()


def _ensure_sqlite_target_dirs(*, target_state_dir: Path, target_db: Path, use_default_dirs: bool) -> None:
    if use_default_dirs:
        paths.ensure_data_dirs()
        return
    target_state_dir.mkdir(parents=True, exist_ok=True)
    target_db.parent.mkdir(parents=True, exist_ok=True)


def resolve_primary_platform_from_config(state_dir: Path | None = None) -> str | None:
    """Best-effort primary platform lookup for store-level SQLite bootstrap."""
    config_paths = _candidate_config_paths(state_dir)
    for config_path in config_paths:
        platform = _resolve_primary_platform_from_config_path(config_path)
        if platform is not None:
            return platform
    return None


def _candidate_config_paths(state_dir: Path | None) -> list[Path]:
    if state_dir is None:
        return [paths.get_config_path()]

    state_path = Path(state_dir).expanduser().resolve()
    candidates: list[Path] = []
    if state_path.name == "state":
        candidates.append(state_path.parent / "config" / "config.json")
    candidates.append(state_path / "config.json")
    return candidates


def _resolve_primary_platform_from_config_path(config_path: Path) -> str | None:
    try:
        if not config_path.exists():
            return None
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    platforms = payload.get("platforms")
    if isinstance(platforms, dict):
        primary = platforms.get("primary")
        if isinstance(primary, str) and primary.strip():
            return primary.strip()

    platform = payload.get("platform")
    if isinstance(platform, str) and platform.strip():
        return platform.strip()
    return None


def _has_import_marker(conn: Connection) -> bool:
    return (
        conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == JSON_IMPORT_MARKER)).scalar_one_or_none()
        is not None
    )


def _has_background_import_marker(conn: Connection) -> bool:
    return (
        conn.execute(
            select(state_meta.c.value_json).where(state_meta.c.key == BACKGROUND_IMPORT_MARKER)
        ).scalar_one_or_none()
        is not None
    )


def _set_import_marker(conn: Connection) -> None:
    now = _utc_now_iso()
    conn.execute(
        state_meta.insert().values(
            key=JSON_IMPORT_MARKER,
            value_json=_json_dumps(now),
            updated_at=now,
        )
    )


def _set_background_import_marker(conn: Connection) -> None:
    now = _utc_now_iso()
    conn.execute(
        state_meta.delete().where(state_meta.c.key == BACKGROUND_IMPORT_MARKER),
    )
    conn.execute(
        state_meta.insert().values(
            key=BACKGROUND_IMPORT_MARKER,
            value_json=_json_dumps(now),
            updated_at=now,
        )
    )


def _run_sqlite_data_migrations(conn: Connection) -> dict[str, int]:
    return {}


def _backup_json_state(state_dir: Path) -> Path:
    backups_dir = state_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backups_dir / f"sqlite-state-migration-{timestamp}"
    suffix = 1
    while backup_path.exists():
        suffix += 1
        backup_path = backups_dir / f"sqlite-state-migration-{timestamp}-{suffix}"
    backup_path.mkdir(parents=True)

    manifest: dict[str, Any] = {"created_at": _utc_now_iso(), "files": {}}
    for name in (
        "settings.json",
        "sessions.json",
        "discovered_chats.json",
        "scheduled_tasks.json",
        "watches.json",
    ):
        source = state_dir / name
        if not source.exists():
            manifest["files"][name] = {"present": False}
            continue
        target = backup_path / name
        shutil.copy2(source, target)
        stat = source.stat()
        manifest["files"][name] = {
            "present": True,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    task_requests = state_dir / "task_requests"
    if task_requests.exists():
        shutil.copytree(task_requests, backup_path / "task_requests")
        manifest["files"]["task_requests"] = {"present": True}
    else:
        manifest["files"]["task_requests"] = {"present": False}

    (backup_path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return backup_path


@dataclass
class _ParsedState:
    settings: SettingsState
    sessions: SessionState
    discovered: DiscoveredChatsStore
    discovered_skipped: bool = False


def _parse_json_state(state_dir: Path, *, primary_platform: str | None) -> _ParsedState:
    settings = _load_settings_from_copy(state_dir / "settings.json")
    sessions = _load_sessions_from_copy(state_dir / "sessions.json", primary_platform=primary_platform)
    discovered, discovered_skipped = _load_discovered_chats_for_import(state_dir / "discovered_chats.json")
    return _ParsedState(
        settings=settings,
        sessions=sessions,
        discovered=discovered,
        discovered_skipped=discovered_skipped,
    )


def _load_settings_from_copy(source: Path) -> SettingsState:
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "settings.json"
        if source.exists():
            shutil.copy2(source, target)
        state, _migrated = load_settings_state_from_json(target)
        return state


def _load_sessions_from_copy(source: Path, *, primary_platform: str | None) -> SessionState:
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "sessions.json"
        if source.exists():
            shutil.copy2(source, target)
        state = load_session_state_from_json(target)
        _migrate_session_state_for_import(state, primary_platform=primary_platform)
        return state


def _load_discovered_chats_for_import(source: Path) -> tuple[DiscoveredChatsStore, bool]:
    if not source.exists():
        return DiscoveredChatsStore(source), False
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        _validate_discovered_chats_payload(payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Skipping discovered_chats.json during SQLite import; settings and sessions will still be migrated: %s",
            exc,
        )
        return DiscoveredChatsStore(source.with_name(f".{source.name}.skipped-for-sqlite-import")), True
    return DiscoveredChatsStore(source), False


def _validate_discovered_chats_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("discovered_chats.json must contain a JSON object")

    raw_platforms = payload.get("platforms", {})
    if raw_platforms is None:
        return
    if not isinstance(raw_platforms, dict):
        raise ValueError("discovered_chats.json platforms must contain a JSON object")

    for platform, chats in raw_platforms.items():
        if not isinstance(chats, dict):
            raise ValueError(f"discovered_chats.json platform {platform!r} must contain a JSON object")
        for chat_id, chat_payload in chats.items():
            if not isinstance(chat_payload, dict):
                raise ValueError(
                    f"discovered_chats.json chat {platform!r}/{chat_id!r} must contain a JSON object"
                )


def _migrate_session_state_for_import(state: SessionState, *, primary_platform: str | None) -> None:
    if primary_platform is not None and not primary_platform.strip():
        raise ValueError("primary_platform must be non-empty when provided")

    needs_default_platform = False
    for data in state.active_polls.values():
        if not isinstance(data, dict) or data.get("platform"):
            continue
        settings_key = data.get("settings_key", "")
        if not isinstance(settings_key, str) or "::" not in settings_key or not settings_key.split("::", 1)[0]:
            needs_default_platform = True
            break

    if not needs_default_platform:
        for scope_key, agent_maps in state.session_mappings.items():
            if "::" in str(scope_key) or not agent_maps:
                continue
            if not infer_platform_from_thread_ids(agent_maps):
                needs_default_platform = True
                break

    if needs_default_platform and primary_platform is None:
        raise ValueError(
            "primary_platform is required to import legacy sessions.json entries that do not encode a platform"
        )

    default_platform = primary_platform or ""
    migrate_session_state_active_polls(state, default_platform)
    migrate_session_state_mappings(state, default_platform)


def _clear_imported_state(conn: Connection) -> None:
    for table in imported_state_tables:
        conn.execute(table.delete())
    conn.execute(state_meta.delete().where(state_meta.c.key == JSON_IMPORT_MARKER))
    conn.execute(state_meta.delete().where(state_meta.c.key == BACKGROUND_IMPORT_MARKER))
    conn.execute(state_meta.delete().where(state_meta.c.key == SESSIONS_LAST_ACTIVITY_KEY))


def _write_parsed_state(db_path: Path, parsed: _ParsedState) -> None:
    settings_service = SQLiteSettingsService(db_path)
    sessions_service = SQLiteSessionsService(db_path)
    try:
        settings_service.save_state(parsed.settings)
        sessions_service.save_state(parsed.sessions)
    finally:
        settings_service.close()
        sessions_service.close()


def _import_discovered_chats(conn: Connection, discovered: DiscoveredChatsStore) -> int:
    now = _utc_now_iso()
    count = 0
    for platform, chats in discovered.state.chats.items():
        for chat_id, chat in chats.items():
            upsert_scope(
                conn,
                str(platform),
                "channel",
                str(chat_id),
                display_name=chat.name,
                native_type=chat.chat_type,
                is_private=chat.is_private,
                supports_threads=chat.supports_topics,
                metadata={
                    "username": chat.username,
                    "is_forum": chat.is_forum,
                    "last_seen_at": chat.last_seen_at,
                },
                now=now,
            )
            count += 1
    return count


def _import_background_state(conn: Connection, state_dir: Path) -> dict[str, int]:
    task_count = _import_scheduled_tasks(conn, state_dir / "scheduled_tasks.json")
    watch_count = _import_watches(conn, state_dir / "watches.json")
    request_count = _import_task_requests(conn, state_dir / "task_requests")
    runtime_count = _import_watch_runtime(conn, state_dir / "watch_runtime.json")
    return {
        "background_scheduled_tasks": task_count,
        "background_watches": watch_count,
        "background_runs_imported": request_count + runtime_count,
    }


def _import_scheduled_tasks(conn: Connection, source: Path) -> int:
    payload = _read_json_object(source)
    count = 0
    for item in payload.get("tasks", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        conn.execute(
            run_definitions.insert().values(
                id=str(item.get("id") or ""),
                definition_type="scheduled",
                name=item.get("name"),
                agent_name=item.get("agent_name"),
                session_policy="existing",
                session_id=item.get("session_id"),
                legacy_session_key=item.get("session_key") or None,
                prompt=item.get("prompt") or "",
                message=item.get("message") or item.get("prompt") or "",
                message_payload_json=None,
                schedule_type=item.get("schedule_type") or "",
                cron=item.get("cron"),
                run_at=item.get("run_at"),
                timezone=item.get("timezone") or "UTC",
                command_json=None,
                shell_command=None,
                prefix=None,
                cwd=None,
                mode=None,
                timeout_seconds=None,
                lifetime_timeout_seconds=None,
                retry_exit_codes_json=None,
                retry_delay_seconds=None,
                post_to=item.get("post_to"),
                deliver_key=item.get("deliver_key"),
                enabled=1 if item.get("enabled", True) else 0,
                deleted_at=None,
                created_at=item.get("created_at") or _utc_now_iso(),
                updated_at=item.get("updated_at") or item.get("created_at") or _utc_now_iso(),
                last_started_at=None,
                last_finished_at=None,
                last_event_at=None,
                last_run_at=item.get("last_run_at"),
                last_error=item.get("last_error"),
                last_exit_code=None,
                last_run_id=None,
                metadata_json=_json_dumps({}),
            )
        )
        count += 1
    return count


def _import_watches(conn: Connection, source: Path) -> int:
    payload = _read_json_object(source)
    count = 0
    for item in payload.get("watches", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        conn.execute(
            run_definitions.insert().values(
                id=str(item.get("id") or ""),
                definition_type="watch",
                name=item.get("name"),
                agent_name=item.get("agent_name"),
                session_policy="existing",
                session_id=item.get("session_id"),
                legacy_session_key=item.get("session_key") or None,
                prompt=None,
                message=item.get("message") or item.get("prefix"),
                message_payload_json=None,
                schedule_type=None,
                cron=None,
                run_at=None,
                timezone=None,
                command_json=_json_dumps(item.get("command") or []),
                shell_command=item.get("shell_command"),
                prefix=item.get("prefix"),
                cwd=item.get("cwd"),
                mode=item.get("mode") or "once",
                timeout_seconds=float(item.get("timeout_seconds", 21600.0)),
                lifetime_timeout_seconds=float(item.get("lifetime_timeout_seconds", 0.0)),
                retry_exit_codes_json=_json_dumps(item.get("retry_exit_codes") or []),
                retry_delay_seconds=float(item.get("retry_delay_seconds", 30.0)),
                post_to=item.get("post_to"),
                deliver_key=item.get("deliver_key"),
                enabled=1 if item.get("enabled", True) else 0,
                deleted_at=None,
                created_at=item.get("created_at") or _utc_now_iso(),
                updated_at=item.get("updated_at") or item.get("created_at") or _utc_now_iso(),
                last_started_at=item.get("last_started_at"),
                last_finished_at=item.get("last_finished_at"),
                last_event_at=item.get("last_event_at"),
                last_run_at=None,
                last_error=item.get("last_error"),
                last_exit_code=item.get("last_exit_code"),
                last_run_id=None,
                metadata_json=_json_dumps({}),
            )
        )
        count += 1
    return count


def _import_task_requests(conn: Connection, root: Path) -> int:
    count = 0
    for status_dir, status in (("pending", "queued"), ("processing", "queued"), ("completed", "succeeded")):
        directory = root / status_dir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            item = _read_json_object(path)
            if not item:
                continue
            ok = item.get("ok")
            run_status = "failed" if status_dir == "completed" and ok is False else status
            created_at = item.get("created_at") or item.get("completed_at") or _utc_now_iso()
            conn.execute(
                agent_runs.insert().values(
                    id=str(item.get("id") or path.stem),
                    definition_id=item.get("definition_id") or item.get("task_id"),
                    run_type=item.get("request_type") or "hook_send",
                    status=run_status,
                    source_kind="scheduler" if item.get("task_id") else "cli",
                    source_actor=None,
                    parent_run_id=None,
                    agent_name=item.get("agent_name"),
                    agent_id=None,
                    agent_backend=item.get("agent_backend"),
                    model=item.get("model"),
                    reasoning_effort=item.get("reasoning_effort"),
                    session_policy="existing" if item.get("session_id") else None,
                    session_id=item.get("session_id"),
                    legacy_session_key=item.get("session_key"),
                    post_to=item.get("post_to"),
                    deliver_key=item.get("deliver_key"),
                    prompt=item.get("prompt"),
                    message=item.get("message") or item.get("prompt"),
                    message_payload_json=None,
                    result_text=None,
                    result_payload_json=None,
                    message_ids_json=None,
                    cancel_requested=0,
                    cancel_requested_at=None,
                    pid=None,
                    exit_code=None,
                    error=item.get("error"),
                    stdout=None,
                    stderr=None,
                    created_at=created_at,
                    started_at=None,
                    completed_at=item.get("completed_at"),
                    updated_at=item.get("completed_at") or created_at,
                    metadata_json=_json_dumps({"ok": ok} if ok is not None else {}),
                )
            )
            count += 1
    return count


def _import_watch_runtime(conn: Connection, source: Path) -> int:
    payload = _read_json_object(source)
    watches = payload.get("watches", {}) if isinstance(payload, dict) else {}
    if not isinstance(watches, dict):
        return 0
    count = 0
    for watch_id, item in watches.items():
        if not isinstance(item, dict):
            continue
        now = item.get("updated_at") or _utc_now_iso()
        conn.execute(
            agent_runs.insert().values(
                id=f"runtime:{watch_id}",
                definition_id=str(watch_id),
                run_type="watch_runtime",
                status="running" if item.get("running") else "completed",
                source_kind="watch",
                source_actor=None,
                parent_run_id=None,
                agent_name=None,
                agent_id=None,
                agent_backend=None,
                model=None,
                reasoning_effort=None,
                session_policy=None,
                session_id=None,
                legacy_session_key=None,
                post_to=None,
                deliver_key=None,
                prompt=None,
                message=None,
                message_payload_json=None,
                result_text=None,
                result_payload_json=None,
                message_ids_json=None,
                cancel_requested=0,
                cancel_requested_at=None,
                pid=item.get("pid"),
                exit_code=None,
                error=None,
                stdout=None,
                stderr=None,
                created_at=item.get("started_at") or now,
                started_at=item.get("started_at"),
                completed_at=None,
                updated_at=now,
                metadata_json=_json_dumps(item),
            )
        )
        count += 1
    return count


def _validate_import(conn: Connection, _counts: dict[str, int]) -> None:
    integrity = conn.exec_driver_sql("PRAGMA integrity_check").scalar_one()
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {integrity}")


def _current_counts(conn: Connection) -> dict[str, int]:
    tables = {
        "agents": agents,
        "scopes": scopes,
        "scope_settings": scope_settings,
        "auth_codes": auth_codes,
        "agent_sessions": agent_sessions,
        "runtime_records": runtime_records,
        "run_definitions": run_definitions,
        "agent_runs": agent_runs,
    }
    return {key: int(conn.execute(select(func.count()).select_from(table)).scalar_one()) for key, table in tables.items()}


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Skipping invalid background JSON state %s: %s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default




def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
