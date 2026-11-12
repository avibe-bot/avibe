from __future__ import annotations

import logging
from pathlib import Path


APPLICATION_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(funcName)s() - %(message)s"
APPLICATION_LOG_MAX_BYTES = 20 * 1024 * 1024
APPLICATION_LOG_BACKUP_COUNT = 5


def configure_library_logging() -> None:
    """HTTP request lines and transport traces can contain credentials/bodies."""
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)


class RedactingFormatter(logging.Formatter):
    """Redact the final rendered record, including cached/chained exceptions.

    Keep this at the output boundary: logger filters do not see child records,
    and filtering only msg/args misses exception text prepared by QueueHandler.
    Payload-producing call sites must still omit bodies and command previews;
    pattern matching cannot recognize arbitrary secrets in unstructured text.
    """

    def format(self, record: logging.LogRecord) -> str:
        from core.agent_core.ai.errors import redact_provider_text

        return redact_provider_text(super().format(record))


def application_log_paths(current_path: Path) -> list[Path]:
    """Return application log generations from oldest to current."""

    return [
        *(current_path.with_name(f"{current_path.name}.{index}") for index in range(APPLICATION_LOG_BACKUP_COUNT, 0, -1)),
        current_path,
    ]
