from __future__ import annotations

from pathlib import Path


APPLICATION_LOG_MAX_BYTES = 20 * 1024 * 1024
APPLICATION_LOG_BACKUP_COUNT = 5


def application_log_paths(current_path: Path) -> list[Path]:
    """Return application log generations from oldest to current."""

    return [
        *(current_path.with_name(f"{current_path.name}.{index}") for index in range(APPLICATION_LOG_BACKUP_COUNT, 0, -1)),
        current_path,
    ]
