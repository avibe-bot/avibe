#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import sys
import logging
import time
from typing import Any

from config.paths import ensure_data_dirs, get_logs_dir
from vibe.logging_config import (
    APPLICATION_LOG_BACKUP_COUNT,
    APPLICATION_LOG_FORMAT,
    APPLICATION_LOG_MAX_BYTES,
    RedactingFormatter,
)
from vibe.service_logging import (
    ServiceFileHandler,
    ServiceStreamHandler,
    configure_service_logging,
    shutdown_service_logging,
)
from vibe.shutdown_diagnostics import ShutdownDiagnostics
from vibe.macos_session_diagnostics import start_macos_session_diagnostics
from vibe.process_limits import raise_open_file_limit
from vibe.runtime import (
    SERVICE_START_FAILURE_DATABASE_DISK_FULL,
    ServiceAlreadyRunningError,
    acquire_service_instance_lock,
    clear_service_start_failure,
    consume_shutdown_intent,
    record_service_start_failure,
    release_service_instance_lock,
    service_start_failure,
    shutdown_intent_required,
)


def _build_logging_handlers(logs_dir: str) -> list[logging.Handler]:
    handlers: list[logging.Handler] = [
        ServiceFileHandler(
            f"{logs_dir}/vibe_remote.log",
            maxBytes=APPLICATION_LOG_MAX_BYTES,
            backupCount=APPLICATION_LOG_BACKUP_COUNT,
        )
    ]
    try:
        if os.environ.get("VIBE_DISABLE_STDOUT_LOGGING", "").lower() not in {"1", "true", "yes"}:
            handlers.insert(0, ServiceStreamHandler(sys.stdout))
    except BaseException:
        for handler in handlers:
            handler.close()
        raise
    return handlers


def setup_logging(level: str = "INFO") -> None:
    """Keep destination I/O on the service logging listener, not the controller."""
    log_level = getattr(logging, level.upper())
    ensure_data_dirs()
    logs_dir = str(get_logs_dir())
    configure_service_logging(
        lambda: _build_logging_handlers(logs_dir), log_level, RedactingFormatter(APPLICATION_LOG_FORMAT),
    )


def load_config() -> Any:
    from config.v2_config import V2Config

    # main() acquires the instance lock first: a newly installed CLI or a
    # rejected second service must not migrate an older live reader's config.
    return V2Config.load(persist_migrations=True)


def prepare_sqlite_state(config: Any):
    """Repair and validate backups once before the service starts.

    Ordinary CLI/store opens probe first. This startup owner must validate its
    own backup attempt even when a CLI/UI process already upgraded the schema,
    so service readiness can still start the existing 72-hour expiry clock.
    """
    from storage.importer import repair_sqlite_state
    from vibe.runtime import service_database_preparation

    with service_database_preparation():
        return repair_sqlite_state(primary_platform=config.platform, include_counts=False)


def _record_database_disk_full(exc: BaseException, logger: logging.Logger) -> None:
    """Leave the user-facing reason behind when the database update ran out of disk."""

    try:
        from storage.migrations import DatabaseDiskFullError

        if not isinstance(exc, DatabaseDiskFullError):
            return
        record_service_start_failure(
            SERVICE_START_FAILURE_DATABASE_DISK_FULL,
            rollback_confirmed=exc.rollback_confirmed is True,
        )
        failure = service_start_failure(os.getpid())
        if failure:
            logger.error(failure["detail"])
    except Exception:
        logger.warning("Could not record the database disk-full startup failure", exc_info=True)


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


def _log_shutdown_intent(logger: logging.Logger, signum: int) -> dict | None:
    if signum != signal.SIGTERM or not shutdown_intent_required():
        return None
    intent = consume_shutdown_intent(os.getpid(), signum)
    if intent is None:
        logger.warning(
            "No managed shutdown intent found for SIGTERM pid=%s; honoring signal",
            os.getpid(),
        )
        return None
    logger.info("Accepted managed shutdown intent: %s", intent)
    return intent


def _should_reap_desktop_runtime_ui(shutdown_intent: dict | None) -> bool:
    return shutdown_intent is None


def _stop_macos_session_diagnostics(monitor: Any, diagnostics: ShutdownDiagnostics) -> None:
    if monitor is None:
        return
    diagnostics.phase("macos_monitor_stop_start")
    try:
        monitor.stop()
    except Exception:
        logging.getLogger(__name__).debug("macOS session diagnostics cleanup failed")
    finally:
        diagnostics.phase("macos_monitor_stop_return")


def _stop_owned_desktop_runtime_ui(logger: logging.Logger) -> None:
    """Reap the UI that belongs to this desktop Runtime before service exit."""

    try:
        from vibe import runtime
        from vibe.desktop_runtime import desktop_caller_provenance

        runtime_ids = desktop_caller_provenance()
        if not runtime_ids:
            return
        if not runtime.stop_ui(stop_remote_access=False, runtime_ids=runtime_ids):
            logger.warning("Desktop Runtime UI did not stop during service shutdown")
    except Exception:
        logger.warning("Failed to stop the desktop Runtime UI during service shutdown", exc_info=True)


def _request_controller_loop_stop(controller: Any) -> bool:
    """Ask a running controller loop to exit so its finally block can clean up."""

    loop = getattr(controller, "_loop", None)
    if loop is None or loop.is_closed() or not loop.is_running():
        return False
    loop.call_soon_threadsafe(loop.stop)
    return True


def main():
    """Main entry point"""
    process_started_at = time.monotonic()
    lock_acquired = False
    macos_session_diagnostics = None
    controller = None
    shutdown_intent = None
    shutdown_diagnostics = ShutdownDiagnostics(get_logs_dir())
    try:
        # Before anything opens files, and inherited by every Agent and worker.
        raise_open_file_limit()
        acquire_service_instance_lock()
        lock_acquired = True
        # Only this start's own failure may be reported for it.
        clear_service_start_failure()

        # Load configuration
        config = load_config()

        # Setup logging
        setup_logging(config.runtime.log_level)
        logger = logging.getLogger(__name__)

        from core.managed_skills import (
            BUILTIN_SKILLS_ROOT_ENV,
            BUILTIN_SKILLS_SNAPSHOT_ENV,
            prepare_builtin_skills,
        )

        try:
            prepare_builtin_skills()
        except Exception:
            os.environ.pop(BUILTIN_SKILLS_SNAPSHOT_ENV, None)
            os.environ[BUILTIN_SKILLS_ROOT_ENV] = ""
            logger.warning("Avibe built-in Skills are unavailable", exc_info=True)

        logger.info("Starting vibe-remote service...")
        logger.info(f"Working directory: {config.runtime.default_cwd}")
        logger.info(
            "Shutdown intent diagnostics enabled=%s env=%s",
            shutdown_intent_required(),
            os.environ.get("VIBE_REQUIRE_SHUTDOWN_INTENT"),
        )
        macos_session_diagnostics = start_macos_session_diagnostics(logger)
        if sys.platform != "darwin":
            from core.process_diagnostics import log_process_snapshot

            log_process_snapshot(logger, "service-start")
        sqlite_started_at = time.monotonic()
        try:
            report = prepare_sqlite_state(config)
            from storage.web_push_cutover import reconcile_startup

            reconcile_startup(config)
        except Exception as exc:
            _record_database_disk_full(exc, logger)
            raise
        logger.info(
            "SQLite state ready: imported=%s db_path=%s backup_path=%s",
            report.imported,
            report.db_path,
            report.backup_path,
        )
        logger.info(
            "Startup phase=sqlite_state duration_ms=%d elapsed_ms=%d",
            round((time.monotonic() - sqlite_started_at) * 1000),
            round((time.monotonic() - process_started_at) * 1000),
        )

        # Create and run controller
        from core.controller import Controller
        from config.v2_compat import to_app_config

        controller_started_at = time.monotonic()
        controller = Controller(to_app_config(config))
        controller._startup_started_at = process_started_at
        logger.info(
            "Startup phase=controller_init duration_ms=%d elapsed_ms=%d",
            round((time.monotonic() - controller_started_at) * 1000),
            round((time.monotonic() - process_started_at) * 1000),
        )

        shutdown_initiated = False

        def _handle_shutdown(signum, frame):
            nonlocal shutdown_initiated, shutdown_intent
            if shutdown_initiated:
                return
            shutdown_initiated = True
            shutdown_diagnostics.start()
            try:
                logger.info("Shutdown diagnostic destination: %s", shutdown_diagnostics.path)
                _log_shutdown_signal(logger, signum)
                shutdown_intent = _log_shutdown_intent(logger, signum)
                logger.info("Shutting down after signal %s", signum)
            except Exception:
                pass
            controller.request_shutdown(f"signal {signum}")

        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)

        # Readiness is published from inside `controller.run()`, by the code that
        # reaches it. Announcing it from here was announcing something this
        # function has not observed: `run()` still has to build the event loop,
        # start the checkpoint service, schedule the internal server and get the
        # IM runtime onto its thread, and it catches its own failures and returns,
        # so the process exits through the ordinary path. A watcher that saw
        # `running` in that window read a release dying in its own startup as an
        # upgrade that worked.
        try:
            controller.run()
        finally:
            shutdown_diagnostics.phase("controller_run_return")
            if _should_reap_desktop_runtime_ui(shutdown_intent):
                _stop_owned_desktop_runtime_ui(logger)
            _stop_macos_session_diagnostics(macos_session_diagnostics, shutdown_diagnostics)
        
    except ServiceAlreadyRunningError as e:
        logging.error("Failed to start: %s", e)
        sys.exit(2)
    except Exception as e:
        logging.error(f"Failed to start: {e}")
        sys.exit(1)
    finally:
        try:
            _stop_macos_session_diagnostics(macos_session_diagnostics, shutdown_diagnostics)
        finally:
            try:
                # Controller.run() has finished its census/watchdog cleanup.
                # Finish rotating/writing before another service can own the log.
                shutdown_diagnostics.phase("logging_drain_start")
                shutdown_service_logging()
                shutdown_diagnostics.phase("logging_drain_return")
            finally:
                try:
                    lock_safe = bool(
                        controller is None
                        or getattr(controller, "service_lock_safe_to_release", True)
                    )
                    if lock_acquired and lock_safe:
                        release_service_instance_lock()
                finally:
                    # This is main's exit boundary, not process-exit evidence:
                    # Python may still be joining workers. Keep diagnostics armed.
                    shutdown_diagnostics.phase("main_return")


if __name__ == "__main__":
    main()
