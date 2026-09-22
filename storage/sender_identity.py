"""Name the human behind a Chat message on an Organization instance.

A ``messages`` row records *which principal* wrote it -- ``author_id`` is
``local`` for a loopback browser and ``remote:<OIDC sub>`` for a
Cloud-authenticated one -- but no column holds anything a reader recognises. On
a personal instance that is enough: every human row is the owner. On an
organization instance several people share one transcript, so a bubble with no
name cannot be attributed.

The only human-readable identity this machine holds for a subject is the email
Cloud stored in ``remote_access_authorizations`` -- the same source Show Pages
read. Resolution happens here, read-side, and only the *label* (the email's
local part) is written onto a payload: the address itself never leaves this
module, so a transcript that is later shared or exported carries a display name
rather than a mailbox.

Attaching at payload-shaping time rather than at each HTTP surface is what
makes a row keep its sender between the ``message.new`` event and the reload
that follows: both go through ``storage.messages_service``.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.engine import Connection

from storage.models import remote_access_authorizations

logger = logging.getLogger(__name__)

REMOTE_PRINCIPAL_PREFIX = "remote:"
SENDER_LABEL_FIELD = "sender_label"


def organization_instance() -> bool:
    """True when this instance is configured as an organization.

    Never raises: an unreadable config means no sender identity, not a failed
    transcript request.
    """
    try:
        from core.services import settings as settings_service

        config = settings_service.load_config()
    except Exception:
        logger.debug("Sender identity: instance kind unavailable", exc_info=True)
        return False
    return config.remote_access.vibe_cloud.instance_kind == "organization"


def sender_label_from_email(email: Any) -> str | None:
    """The display label for a stored email: its local part.

    An address with an empty local part falls back to the whole string so a
    reader still gets something stable rather than a blank avatar.
    """
    if not isinstance(email, str):
        return None
    trimmed = email.strip()
    if not trimmed:
        return None
    return trimmed.split("@", 1)[0].strip() or trimmed


def resolve_sender_labels(conn: Connection, subjects: Iterable[str]) -> dict[str, str]:
    """Map OIDC subjects to display labels in one query.

    A subject can hold several authorization rows (one per granted scope); they
    describe the same person, so the most recently written one wins and ties
    break on ``id`` -- the same subject must not label differently between two
    reads of the same transcript.
    """
    wanted = {subject for subject in subjects if subject}
    if not wanted:
        return {}
    try:
        rows = list(
            conn.execute(
                select(
                    remote_access_authorizations.c.id,
                    remote_access_authorizations.c.subject,
                    remote_access_authorizations.c.email,
                    remote_access_authorizations.c.updated_at,
                    remote_access_authorizations.c.created_at,
                ).where(remote_access_authorizations.c.subject.in_(wanted))
            ).mappings()
        )
    except Exception:
        logger.debug("Sender identity: authorization lookup failed", exc_info=True)
        return {}

    freshest: dict[str, Any] = {}
    for row in rows:
        subject = row["subject"]
        key = (row["updated_at"] or row["created_at"] or 0, row["id"] or "")
        current = freshest.get(subject)
        if current is None or key > current[0]:
            freshest[subject] = (key, row["email"])
    return {
        subject: label
        for subject, (_key, email) in freshest.items()
        if (label := sender_label_from_email(email))
    }


def attach_sender_labels(
    conn: Connection,
    payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add ``sender_label`` to the remote-authored rows of one page.

    Cheap by construction: a page with no ``remote:`` author -- every personal
    instance, and every loopback or agent row on an organization one -- returns
    before the config read and before the query. A row whose subject has no
    stored email simply gets no field, and the UI renders it as an unknown
    sender.
    """
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for payload in payloads:
        author_id = payload.get("author_id")
        if not isinstance(author_id, str) or not author_id.startswith(REMOTE_PRINCIPAL_PREFIX):
            continue
        subject = author_id[len(REMOTE_PRINCIPAL_PREFIX) :].strip()
        if subject:
            by_subject.setdefault(subject, []).append(payload)
    if not by_subject or not organization_instance():
        return payloads

    for subject, label in resolve_sender_labels(conn, by_subject.keys()).items():
        for payload in by_subject[subject]:
            payload[SENDER_LABEL_FIELD] = label
    return payloads
