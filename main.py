#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import sys
import logging
from logging.handlers import RotatingFileHandler
from typing import Any

from config.paths import ensure_data_dirs, get_logs_dir
from vibe.runtime import (
    ServiceAlreadyRunningError,
    acquire_service_instance_lock,
    consume_shutdown_intent,
    release_service_instance_lock,
    shutdown_intent_required,
)


APPLICATION_LOG_MAX_BYTES = 20 * 1024 * 1024
APPLICATION_LOG_BACKUP_COUNT = 5


def _build_logging_handlers(logs_dir: str) -> list[logging.Handler]:
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            f"{logs_dir}/vibe_remote.log",
            maxBytes=APPLICATION_LOG_MAX_BYTES,
            backupCount=APPLICATION_LOG_BACKUP_COUNT,
        )
    ]
    if os.environ.get("VIBE_DISABLE_STDOUT_LOGGING", "").lower() not in {"1", "true", "yes"}:
        handlers.insert(0, logging.StreamHandler(sys.stdout))
    return handlers


def setup_logging(level: str = "INFO"):
    """Setup logging configuration with file location and line numbers"""
    # Create a custom formatter with file location
    log_format = '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(funcName)s() - %(message)s'
    
    # For development, you can use this more detailed format:
    # log_format = '%(asctime)s - %(name)s - %(levelname)s - [%(pathname)s:%(lineno)d] - %(funcName)s() - %(message)s'
    
    ensure_data_dirs()
    logs_dir = str(get_logs_dir())

    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=log_format,
        handlers=_build_logging_handlers(logs_dir),
    )


def apply_claude_sdk_patches():
    """Apply runtime patches for third-party SDK limits."""
    logger = logging.getLogger(__name__)
    try:
        from modules.claude_sdk_compat import CLAUDE_SDK_MAX_BUFFER_SIZE
        from claude_agent_sdk._internal.transport import subprocess_cli
    except Exception as exc:
        logger.warning(f"Claude SDK patch skipped: {exc}")
        return

    patched = []
    for attr in ("_DEFAULT_MAX_BUFFER_SIZE", "_MAX_BUFFER_SIZE"):
        if not hasattr(subprocess_cli, attr):
            continue
        previous = getattr(subprocess_cli, attr)
        setattr(subprocess_cli, attr, CLAUDE_SDK_MAX_BUFFER_SIZE)
        if previous != CLAUDE_SDK_MAX_BUFFER_SIZE:
            patched.append(f"{attr} from {previous} to {CLAUDE_SDK_MAX_BUFFER_SIZE} bytes")

    if patched:
        logger.info(
            "Patched claude_agent_sdk buffer limits: %s",
            ", ".join(patched),
        )


def load_config() -> Any:
    from config.v2_config import V2Config

    return V2Config.load()


def ensure_sqlite_state(*args, **kwargs):
    from storage.importer import ensure_sqlite_state as _ensure_sqlite_state

    return _ensure_sqlite_state(*args, **kwargs)


def prepare_sqlite_state(config: Any):
    """Run safe SQLite state migrations before the service starts."""
    return ensure_sqlite_state(primary_platform=config.platform)


def _log_shutdown_signal(logger: logging.Logger, signum: int) -> None:
    try:
        logger.info(
            "Received signal %s pid=%s ppid=%s pgid=%s sid=%s",
            signum,
            os.getpid(),
            os.getppid(),
            os.getpgid(0),
            os.getsid(0),
        )
    except Exception:
        logger.info("Received signal %s", signum)


def _log_shutdown_intent(logger: logging.Logger, signum: int) -> None:
    if signum != signal.SIGTERM or not shutdown_intent_required():
        return
    intent = consume_shutdown_intent(os.getpid(), signum)
    if intent is None:
        logger.warning(
            "No managed shutdown intent found for SIGTERM pid=%s; honoring signal",
            os.getpid(),
        )
        return
    logger.info("Accepted managed shutdown intent: %s", intent)


def main():
    """Main entry point"""
    lock_acquired = False
    try:
        acquire_service_instance_lock()
        lock_acquired = True

        # Load configuration
        config = load_config()

        # Setup logging
        setup_logging(config.runtime.log_level)
        logger = logging.getLogger(__name__)

        apply_claude_sdk_patches()
        from vibe.sentry_integration import init_sentry

        init_sentry(config, component="service")
        
        logger.info("Starting vibe-remote service...")
        logger.info(f"Working directory: {config.runtime.default_cwd}")
        logger.info(
            "Shutdown intent diagnostics enabled=%s env=%s",
            shutdown_intent_required(),
            os.environ.get("VIBE_REQUIRE_SHUTDOWN_INTENT"),
        )
        from core.process_diagnostics import log_process_snapshot

        log_process_snapshot(logger, "service-start")
        report = prepare_sqlite_state(config)
        logger.info(
            "SQLite state ready: imported=%s db_path=%s backup_path=%s",
            report.imported,
            report.db_path,
            report.backup_path,
        )
        
        # Create and run controller
        from core.controller import Controller
        from config.v2_compat import to_app_config

        controller = Controller(to_app_config(config))

        shutdown_initiated = False

        def _handle_shutdown(signum, frame):
            nonlocal shutdown_initiated
            if shutdown_initiated:
                return
            shutdown_initiated = True
            try:
                _log_shutdown_signal(logger, signum)
                _log_shutdown_intent(logger, signum)
                logger.info("Shutting down after signal %s", signum)
            except Exception:
                pass
            try:
                controller.cleanup_sync()
            except Exception as cleanup_err:
                logger.error(f"Cleanup failed: {cleanup_err}")
            finally:
                release_service_instance_lock()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)

        try:
            controller.run()
        finally:
            release_service_instance_lock()
        
    except ServiceAlreadyRunningError as e:
        logging.error("Failed to start: %s", e)
        sys.exit(2)
    except Exception as e:
        if lock_acquired:
            release_service_instance_lock()
        logging.error(f"Failed to start: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
