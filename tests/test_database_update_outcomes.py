"""What the user is told about the epoch-3 database update, at the surfaces they read.

* No backup kept on a nearly full disk: one workspace notice, in Product's copy;
  the record stays as backup provenance and is only acknowledged.
* The update ran out of disk and rolled back: the service records why it stopped,
  and ``vibe status`` reports that sentence instead of "the process exited".
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from sqlalchemy import select

import main as service_main
from config import paths
from core import storage_notices
from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.migrations import DatabaseDiskFullError
from storage.models import agent_sessions, messages, state_meta
from vibe import runtime
from vibe.i18n import t as i18n_t

ZH_NOTICE = (
    "磁盘空间快满了，这次更新数据库时没有另存备份。你仍然可以装回 3.2.6；"
    "3.2.5 及更早的版本无法再打开这个数据库。建议腾出一些磁盘空间。"
)
EN_NOTICE = (
    "The disk is nearly full, so Avibe updated its database without keeping a backup copy. "
    "You can still go back to 3.2.6; 3.2.5 and older can no longer open this database. "
    "Freeing some disk space is recommended."
)


def _migrated_db(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    return paths.get_sqlite_state_path()


def _mark_backup_skipped(db_path: Path) -> None:
    # The shape the 0089 conversion writes inside its own transaction.
    with create_sqlite_engine(db_path).begin() as conn:
        conn.execute(state_meta.insert().values(
            key=storage_notices.EPOCH3_BACKUP_SKIPPED_KEY,
            value_json=json.dumps({"converted_at": "2026-11-16T07:00:00Z", "database_bytes": 8_100_000_000}),
            updated_at="2026-11-16T07:00:00Z",
        ))


def _notices(db_path: Path) -> list[tuple[str, str]]:
    with create_sqlite_engine(db_path).connect() as conn:
        return [tuple(row) for row in conn.execute(
            select(messages.c.type, messages.c.content_text)
            .where(messages.c.session_id == WORKSPACE_NOTICE_SESSION_ID)
        )]


def _record(db_path: Path) -> dict:
    with create_sqlite_engine(db_path).connect() as conn:
        rows = conn.execute(
            select(state_meta.c.value_json).where(state_meta.c.key == storage_notices.EPOCH3_BACKUP_SKIPPED_KEY)
        ).scalars().all()
    assert len(rows) == 1, "the no-backup record is provenance and is never removed"
    return json.loads(rows[0])


def _forget_acknowledgement(db_path: Path) -> None:
    """A start that posted the notice and stopped before acknowledging it."""
    record = {key: value for key, value in _record(db_path).items() if key != "notified_at"}
    with create_sqlite_engine(db_path).begin() as conn:
        conn.execute(
            state_meta.update().where(state_meta.c.key == storage_notices.EPOCH3_BACKUP_SKIPPED_KEY)
            .values(value_json=json.dumps(record))
        )


def _deliver(language: str) -> bool:
    return asyncio.run(storage_notices.deliver_storage_notices(lambda key, **kw: i18n_t(key, language, **kw)))


def test_the_notice_copy_is_products_wording_in_both_languages():
    assert i18n_t(storage_notices.EPOCH3_BACKUP_SKIPPED_I18N_KEY, "en") == EN_NOTICE
    assert i18n_t(storage_notices.EPOCH3_BACKUP_SKIPPED_I18N_KEY, "zh") == ZH_NOTICE


def test_no_backup_notice_is_shown_once_in_workspace_notices(monkeypatch, tmp_path):
    db_path = _migrated_db(monkeypatch, tmp_path)
    _mark_backup_skipped(db_path)

    assert _deliver("zh") is True
    assert _notices(db_path) == [("notify", ZH_NOTICE)]
    acknowledged = _record(db_path)
    assert acknowledged["notified_at"]
    assert (acknowledged["converted_at"], acknowledged["database_bytes"]) == ("2026-11-16T07:00:00Z", 8_100_000_000)

    # Acknowledged: later starts post nothing and leave the record as it is.
    assert _deliver("zh") is False
    assert _notices(db_path) == [("notify", ZH_NOTICE)]
    assert _record(db_path) == acknowledged

    # Posted but not acknowledged (a crash in between): the next start finds the
    # message already there, says nothing more, and acknowledges it.
    _forget_acknowledgement(db_path)
    assert _deliver("zh") is True
    assert _notices(db_path) == [("notify", ZH_NOTICE)]
    assert _record(db_path)["notified_at"]


def test_without_the_record_no_notice_session_is_created(monkeypatch, tmp_path):
    db_path = _migrated_db(monkeypatch, tmp_path)

    assert _deliver("en") is False
    with create_sqlite_engine(db_path).connect() as conn:
        assert conn.execute(
            select(agent_sessions.c.id).where(agent_sessions.c.id == WORKSPACE_NOTICE_SESSION_ID)
        ).first() is None


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))


@pytest.mark.parametrize(
    ("confirmed", "detail"),
    [(True, runtime.DATABASE_DISK_FULL_DETAIL), (False, runtime.DATABASE_DISK_FULL_UNCONFIRMED_DETAIL)],
)
def test_disk_full_update_is_what_vibe_status_reports(monkeypatch, isolated_home, confirmed, detail):
    monkeypatch.setattr(
        runtime, "resolve_service_state",
        lambda **kwargs: runtime.ServiceState("stopped", "not running", None, None, []),
    )

    # The backend's own type, so a rename or signature change fails here.
    service_main._record_database_disk_full(
        DatabaseDiskFullError(rollback_confirmed=confirmed), service_main.logging.getLogger("test"),
    )

    status = json.loads(runtime.render_status(detect_extra_processes=False))
    assert (status["state"], status["detail"], status["code"]) == ("error", detail, "database_disk_full")
    assert ("Nothing was changed" in detail) is confirmed
    assert runtime.service_exited_detail(runtime.os.getpid()) == detail
    # A start that does not name that process, or began after the failure, is not told it.
    assert runtime.service_start_failure(runtime.os.getpid() + 1) is None
    assert runtime.service_start_failure(since=time.time() + 60) is None


def test_other_startup_failures_record_nothing(isolated_home):

    service_main._record_database_disk_full(RuntimeError("migration failed"), service_main.logging.getLogger("test"))

    assert runtime.service_start_failure() is None
    assert runtime.service_exited_detail(runtime.os.getpid()) == "service process exited before startup completed"


def test_the_service_log_line_is_the_english_notice(caplog):
    with caplog.at_level("WARNING", logger="core.storage_notices"):
        storage_notices.log_epoch3_backup_skipped()
    assert [record.getMessage() for record in caplog.records] == [EN_NOTICE]
