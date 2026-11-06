from .paths import (
    AVIBE_HOME_ENV,
    get_vibe_remote_dir,
    get_config_dir,
    get_state_dir,
    get_logs_dir,
    get_runtime_dir,
    get_runtime_pid_path,
    get_runtime_ui_pid_path,
    get_runtime_status_path,
    get_runtime_doctor_path,
    get_config_path,
    get_settings_path,
    get_sessions_path,
    ensure_data_dirs,
    migrate_default_home,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .v2_config import V2Config
    from .v2_settings import SettingsStore
    from .v2_sessions import SessionsStore


def __getattr__(name: str):
    # Importing config.paths must not also load every configuration consumer.
    if name == "V2Config":
        from .v2_config import V2Config

        return V2Config
    if name == "SettingsStore":
        from .v2_settings import SettingsStore

        return SettingsStore
    if name == "SessionsStore":
        from .v2_sessions import SessionsStore

        return SessionsStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "V2Config",
    "SettingsStore",
    "SessionsStore",
    "AVIBE_HOME_ENV",
    "get_vibe_remote_dir",
    "get_config_dir",
    "get_state_dir",
    "get_logs_dir",
    "get_runtime_dir",
    "get_runtime_pid_path",
    "get_runtime_ui_pid_path",
    "get_runtime_status_path",
    "get_runtime_doctor_path",
    "get_config_path",
    "get_settings_path",
    "get_sessions_path",
    "ensure_data_dirs",
    "migrate_default_home",
]
