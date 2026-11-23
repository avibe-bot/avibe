#!/usr/bin/env python3
"""Check what a user is told after an epoch-3 conversion on a real fixture.

``probe``: the no-backup notice.

Run after a low-space conversion that kept no backup copy. Calls the same
emitter the service starts (``core.storage_notices.deliver_storage_notices``)
several times against that fixture's own database. Then checks that the
workspace notifications hold exactly one notice in the approved copy, and that
the conversion record is still there with a stable ``notified_at``.

``disk_full_outcome``: given the ``storage.migrations.DatabaseDiskFullError``
actually caught from a fixture upgrade, runs the service's own failure-recording
path and returns what ``vibe status`` then reports, with ``ok`` true only when
that is the exact user-facing outcome for the error. Call it while the test
filler is still in place, before freeing it.

Runs only inside the harness's isolated environment: ``HOME`` must be
``<root>/home`` and ``AVIBE_HOME`` must be ``<root>/avibe``. A fixture larger than
16 MiB also requires the bounded Incus guest that the measurement harness requires.
Starts no service, UI, browser or network access, and touches nothing outside the
fixture.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def _guard(root: Path, quota_bytes: int) -> None:
    if os.environ.get("HOME") != str(root / "home") or os.environ.get("AVIBE_HOME") != str(root / "avibe"):
        raise RuntimeError("The probe requires the harness's isolated HOME/AVIBE_HOME environment")
    measure = runpy.run_path(str(Path(__file__).with_name("sqlite_epoch3_measure.py")))
    if measure["fixture_bytes"](root / "avibe") > 16 * MIB:
        measure["require_guest"](root, quota_bytes)


def probe(root: Path, *, language: str = "en", repeat: int = 3, quota_bytes: int = 0) -> dict:
    """Deliver ``repeat`` times, then report what a user would find."""

    root = root.expanduser().resolve()
    _guard(root, quota_bytes)
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import select

    from config import paths
    from core import storage_notices
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.db import get_cached_sqlite_engine
    from storage.models import messages, state_meta
    from vibe.i18n import t

    database = paths.get_sqlite_state_path()
    if not database.is_relative_to(root / "avibe") or not database.is_file():
        raise RuntimeError(f"No converted fixture database under {root / 'avibe'}")

    def record() -> dict | None:
        with get_cached_sqlite_engine().connect() as conn:
            value = conn.execute(
                select(state_meta.c.value_json).where(state_meta.c.key == storage_notices.EPOCH3_BACKUP_SKIPPED_KEY)
            ).scalar_one_or_none()
        return json.loads(value) if value is not None else None

    def notices() -> list[str]:
        with get_cached_sqlite_engine().connect() as conn:
            return list(conn.execute(
                select(messages.c.content_text).where(
                    messages.c.session_id == WORKSPACE_NOTICE_SESSION_ID,
                    messages.c.native_message_id == storage_notices.EPOCH3_BACKUP_SKIPPED_MESSAGE_ID,
                )
            ).scalars())

    before = record()
    delivered = [
        asyncio.run(storage_notices.deliver_storage_notices(lambda key, **kw: t(key, language, **kw)))
        for _ in range(repeat)
    ]
    after = record()
    posted = notices()
    expected = t(storage_notices.EPOCH3_BACKUP_SKIPPED_I18N_KEY, language)
    provenance_kept = bool(
        before and after
        and {key: value for key, value in after.items() if key != "notified_at"}
        == {key: value for key, value in before.items() if key != "notified_at"}
    )
    checks = {
        "record_present_before": before is not None,
        "one_notice": len(posted) == 1,
        "approved_copy": posted == [expected],
        "record_kept_with_notified_at": bool(after and after.get("notified_at")),
        "provenance_unchanged": provenance_kept,
        # The first call posts it, or acknowledges one an earlier start posted;
        # every later call finds it acknowledged and does nothing.
        "later_calls_quiet": delivered[1:] == [False] * (repeat - 1),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "delivered": delivered,
        "record_before": before,
        "record_after": after,
        "language": language,
        "database": str(database),
    }


def disk_full_outcome(root: Path, error: BaseException, *, quota_bytes: int = 0, clear: bool = False) -> dict:
    """Record ``error`` the way the service does, then read it back as the user sees it.

    ``error`` must be the exception the upgrade actually raised. ``clear`` removes
    the fixture's own failure record after reading it.
    """

    root = root.expanduser().resolve()
    _guard(root, quota_bytes)
    sys.path.insert(0, str(ROOT))
    import logging

    import main as service_main
    from storage.migrations import DatabaseDiskFullError
    from vibe import runtime

    if not isinstance(error, DatabaseDiskFullError):
        raise TypeError(f"Expected the caught DatabaseDiskFullError, got {type(error).__name__}")
    service_main._record_database_disk_full(error, logging.getLogger("sqlite_epoch3_notice_probe"))
    failure = runtime.service_start_failure(os.getpid()) or {}
    status = json.loads(runtime.render_status(detect_extra_processes=False))
    if clear:
        runtime.clear_service_start_failure()
    confirmed = error.rollback_confirmed is True
    expected_detail = runtime.DATABASE_DISK_FULL_DETAIL if confirmed else runtime.DATABASE_DISK_FULL_UNCONFIRMED_DETAIL
    result = {
        "error": str(error),
        "rollback_confirmed": error.rollback_confirmed,
        "code": failure.get("code"),
        "detail": failure.get("detail"),
        "desktop_reason": failure.get("receipt_reason"),
        "status": {key: status.get(key) for key in ("state", "detail", "code")},
    }
    # What the user must see for this exact error, checked here so callers assert one flag.
    result["ok"] = (
        result["code"] == "database_disk_full"
        and result["detail"] == expected_detail
        and result["desktop_reason"] == ("database_disk_full" if confirmed else "database_disk_full_unconfirmed")
        and result["status"] == {"state": "error", "detail": expected_detail, "code": "database_disk_full"}
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="the fixture root; HOME and AVIBE_HOME live under it")
    parser.add_argument("--language", choices=("en", "zh"), default="en")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--quota-bytes", type=int, default=0)
    args = parser.parse_args(argv)
    if args.repeat < 2:
        parser.error("--repeat must be at least 2 to prove later calls stay quiet")
    result = probe(args.root, language=args.language, repeat=args.repeat, quota_bytes=args.quota_bytes)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
