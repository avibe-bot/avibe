"""One-time notices the storage layer leaves for the user.

The epoch-3 conversion may proceed without a backup copy on a nearly full disk
that cannot clone. Inside its own transaction it records that fact under
``EPOCH3_BACKUP_SKIPPED_KEY`` in ``state_meta``. That record is permanent backup
provenance and is never deleted here. After startup this module posts one message
to the workspace-notifications Session, the same place failed background work is
reported. Once that message exists, it adds ``notified_at`` to the record. A stable
native message id keeps a crash between posting and marking from showing it twice.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select

logger = logging.getLogger(__name__)

EPOCH3_BACKUP_SKIPPED_KEY = "storage.epoch3_backup_skipped"
EPOCH3_BACKUP_SKIPPED_MESSAGE_ID = "storage-epoch3-backup-skipped"
EPOCH3_BACKUP_SKIPPED_I18N_KEY = "storage.notice.epoch3BackupSkipped"


def log_epoch3_backup_skipped() -> None:
    """The service-log line, written once by the conversion that kept no backup."""

    from vibe.i18n import t

    logger.warning(t(EPOCH3_BACKUP_SKIPPED_I18N_KEY, "en"))


def _record(value_json: str | None) -> dict | None:
    try:
        record = json.loads(value_json) if value_json is not None else None
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _pending() -> bool:
    """A no-backup conversion whose notice has not been acknowledged yet."""

    from storage.db import get_cached_sqlite_engine
    from storage.models import state_meta

    with get_cached_sqlite_engine().connect() as conn:
        value = conn.execute(
            select(state_meta.c.value_json).where(state_meta.c.key == EPOCH3_BACKUP_SKIPPED_KEY)
        ).scalar_one_or_none()
    if value is None:
        return False
    record = _record(value)
    # An unreadable record still means no backup was kept: tell the user. The
    # message id keeps every later attempt from repeating it.
    return record is None or not record.get("notified_at")


def _workspace_session(title: str) -> str:
    from storage.agent_session_rows import resolve_workspace_notice_session
    from storage.db import get_cached_sqlite_engine

    with get_cached_sqlite_engine().begin() as conn:
        return resolve_workspace_notice_session(conn, title=title)


def _delivered_then_acknowledge() -> bool:
    """Stamp ``notified_at`` once the message exists; the record itself stays."""

    from storage.db import get_cached_sqlite_engine
    from storage.models import messages, state_meta

    with get_cached_sqlite_engine().begin() as conn:
        delivered = conn.execute(
            select(messages.c.id).where(
                messages.c.platform == "avibe",
                messages.c.scope_id.is_(None),
                messages.c.native_message_id == EPOCH3_BACKUP_SKIPPED_MESSAGE_ID,
            )
        ).first() is not None
        if not delivered:
            return False
        value = conn.execute(
            select(state_meta.c.value_json).where(state_meta.c.key == EPOCH3_BACKUP_SKIPPED_KEY)
        ).scalar_one_or_none()
        record = _record(value)
        if record is None:
            # Never overwrite provenance this module cannot read.
            logger.warning("The database-update record is unreadable; its notice was posted but not acknowledged")
            return True
        if not record.get("notified_at"):
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                state_meta.update()
                .where(state_meta.c.key == EPOCH3_BACKUP_SKIPPED_KEY)
                .values(value_json=json.dumps({**record, "notified_at": now}, sort_keys=True), updated_at=now)
            )
        return True


async def deliver_storage_notices(translate: Callable[..., str]) -> bool:
    """Post a pending storage notice once; True when one was delivered."""

    from core.message_mirror import persist_agent_message_async
    from modules.im.base import MessageContext

    try:
        if not await asyncio.to_thread(_pending):
            return False
        session_id = await asyncio.to_thread(_workspace_session, translate("harness.notice.workspaceSession"))
        context = MessageContext(
            user_id="avibe",
            channel_id=session_id,
            platform="avibe",
            platform_specific={"agent_session_id": session_id},
        )
        # Returns None for a duplicate id: a previous start posted it and stopped
        # before acknowledging. The check below counts that as delivered.
        await persist_agent_message_async(
            context,
            "notify",
            translate(EPOCH3_BACKUP_SKIPPED_I18N_KEY),
            native_message_id=EPOCH3_BACKUP_SKIPPED_MESSAGE_ID,
        )
        return await asyncio.to_thread(_delivered_then_acknowledge)
    except Exception:
        # Not acknowledged; the next start tries again.
        logger.warning("Could not post the database-update notice", exc_info=True)
        return False
