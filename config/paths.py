import os
import sys
from pathlib import Path


AVIBE_HOME_ENV = "AVIBE_HOME"
AVIBE_HOME_DIRNAME = ".avibe"
LEGACY_HOME_DIRNAME = ".vibe_remote"
HOME_MIGRATION_NOTICE_PATH = "state/home_migration_notice"


def _expand_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _default_avibe_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / AVIBE_HOME_DIRNAME


def _legacy_vibe_remote_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / LEGACY_HOME_DIRNAME


def _is_symlink_to(path: Path, target: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve() == target.resolve()
    except OSError:
        return False


def _safe_symlink_legacy_home(target: Path, legacy: Path) -> bool:
    if legacy.exists() or legacy.is_symlink():
        return _is_symlink_to(legacy, target)
    try:
        legacy.symlink_to(target, target_is_directory=True)
    except OSError:
        return False
    return True


def _write_migration_notice(root: Path, message: str) -> bool:
    notice_path = root / HOME_MIGRATION_NOTICE_PATH
    if notice_path.exists():
        return False
    try:
        notice_path.parent.mkdir(parents=True, exist_ok=True)
        notice_path.write_text(message.rstrip() + "\n", encoding="utf-8")
    except OSError:
        return False
    print(message, file=sys.stderr)
    return True


def get_vibe_remote_dir() -> Path:
    custom = os.environ.get(AVIBE_HOME_ENV)
    if custom:
        return _expand_path(custom)

    avibe_dir = _default_avibe_dir()
    legacy_dir = _legacy_vibe_remote_dir()
    if avibe_dir.exists() or avibe_dir.is_symlink():
        return avibe_dir
    if legacy_dir.exists() or legacy_dir.is_symlink():
        return legacy_dir
    return avibe_dir


def migrate_default_home() -> Path:
    """Adopt the default avibe home while preserving legacy path compatibility.

    Explicit ``AVIBE_HOME`` is honored as-is and never migrated. The migration
    only applies to default homes under ``Path.home()``.
    """
    if os.environ.get(AVIBE_HOME_ENV):
        return get_vibe_remote_dir()

    avibe_dir = _default_avibe_dir()
    legacy_dir = _legacy_vibe_remote_dir()

    if avibe_dir.exists() or avibe_dir.is_symlink():
        _safe_symlink_legacy_home(avibe_dir, legacy_dir)
        if legacy_dir.exists() and not _is_symlink_to(legacy_dir, avibe_dir) and legacy_dir != avibe_dir:
            _write_migration_notice(
                avibe_dir,
                "avibe is using ~/.avibe. A real ~/.vibe_remote directory also exists and was not modified.",
            )
        return avibe_dir

    if legacy_dir.exists() and not legacy_dir.is_symlink():
        try:
            legacy_dir.rename(avibe_dir)
        except OSError:
            return legacy_dir
        _safe_symlink_legacy_home(avibe_dir, legacy_dir)
        _write_migration_notice(
            avibe_dir,
            "Migrated runtime home from ~/.vibe_remote to ~/.avibe. The old path now points to ~/.avibe.",
        )
        return avibe_dir

    if legacy_dir.is_symlink():
        return legacy_dir.resolve()

    return avibe_dir


def get_config_dir() -> Path:
    return get_vibe_remote_dir() / "config"


def get_state_dir() -> Path:
    return get_vibe_remote_dir() / "state"


def get_logs_dir() -> Path:
    return get_vibe_remote_dir() / "logs"


def get_runtime_dir() -> Path:
    return get_vibe_remote_dir() / "runtime"


def get_attachments_dir() -> Path:
    return get_vibe_remote_dir() / "attachments"


def get_show_pages_dir() -> Path:
    return get_vibe_remote_dir() / "show"


def get_show_page_dir(session_id: str) -> Path:
    return get_show_pages_dir() / session_id


def get_runtime_pid_path() -> Path:
    return get_runtime_dir() / "vibe.pid"


def get_runtime_service_lock_path() -> Path:
    return get_runtime_dir() / "service.lock"


def get_runtime_restart_status_path() -> Path:
    return get_runtime_dir() / "restart_status.json"


def get_runtime_ui_pid_path() -> Path:
    return get_runtime_dir() / "vibe-ui.pid"


def get_runtime_remote_access_pid_path() -> Path:
    return get_runtime_dir() / "remote-access-cloudflared.pid"


def get_runtime_status_path() -> Path:
    return get_runtime_dir() / "status.json"


def get_runtime_doctor_path() -> Path:
    return get_runtime_dir() / "doctor.json"


def get_config_path() -> Path:
    return get_config_dir() / "config.json"


def get_settings_path() -> Path:
    return get_state_dir() / "settings.json"


def get_sessions_path() -> Path:
    return get_state_dir() / "sessions.json"


def get_watches_path() -> Path:
    return get_state_dir() / "watches.json"


def get_watch_runtime_path() -> Path:
    return get_runtime_dir() / "watch_runtime.json"


def get_discovered_chats_path() -> Path:
    return get_state_dir() / "discovered_chats.json"


def get_sqlite_state_path() -> Path:
    return get_state_dir() / "vibe.sqlite"


def get_sqlite_migration_lock_path() -> Path:
    return get_state_dir() / "migration.lock"


def get_state_backups_dir() -> Path:
    return get_state_dir() / "backups"


def get_user_preferences_path() -> Path:
    return get_state_dir() / "user_preferences.md"


_USER_PREFERENCES_TEMPLATE = """# User Context and Preferences

Use this file for durable user context, stable preferences, and recurring working patterns.
Prefer adding notes under `## Users`.
Keep entries short, factual, reusable, deduplicated, and free of secrets unless the user explicitly asks.

## Users
### platform/user_id
- Add stable notes about how this user prefers to communicate, work, and make decisions.

## Shared
- Add cross-user notes here only when they are genuinely reusable.
"""


def ensure_data_dirs() -> None:
    from storage.migrations import guard_source_checkout_default_state_bootstrap

    guard_source_checkout_default_state_bootstrap()
    migrate_default_home()
    get_config_dir().mkdir(parents=True, exist_ok=True)
    get_state_dir().mkdir(parents=True, exist_ok=True)
    get_logs_dir().mkdir(parents=True, exist_ok=True)
    get_runtime_dir().mkdir(parents=True, exist_ok=True)
    get_attachments_dir().mkdir(parents=True, exist_ok=True)
    get_show_pages_dir().mkdir(parents=True, exist_ok=True)
    get_state_backups_dir().mkdir(parents=True, exist_ok=True)
    preferences_path = get_user_preferences_path()
    if not preferences_path.exists():
        preferences_path.write_text(_USER_PREFERENCES_TEMPLATE, encoding="utf-8")
