"""Optional, resumable trigram acceleration for the authoritative Message ledger.

Writers only enqueue ids in ordinary SQLite tables (including older binaries).
A post-readiness worker builds a contentless FTS5 index in short transactions.
Search always checks the real ledger, and includes pending journal ids: index
lag can cost work but cannot hide a new match or bypass an access predicate.
"""

from __future__ import annotations

import logging
import time
from threading import Event

from sqlalchemy import Engine, text
from sqlalchemy.sql.selectable import Subquery
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from storage.db import _is_sqlite_busy

logger = logging.getLogger(__name__)

# Keep writer-lock occupancy small, even for messages with large rendered text.
BATCH_SIZE = 16
# Oversized text stays searchable via the original predicate, not a truncated
# index. Bound tokenization and Python allocations even for pathological rows.
MAX_INDEX_CHARACTERS = 64 * 1024
_TEXT_PREFIX = f"lower(substr(content_text, 1, {MAX_INDEX_CHARACTERS + 1}))"
PASS_SECONDS = 5.0
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_FTS_DDL = """CREATE VIRTUAL TABLE message_search_fts USING fts5(
    content_text, content='', contentless_delete=1,
    tokenize='trigram case_sensitive 1', detail=none
)"""


def _available(conn: Connection) -> bool:
    # contentless_delete avoids storing a second copy of all message text. Older
    # SQLite can still read/write the ledger and journal; it just uses LIKE.
    version = tuple(int(part) for part in conn.exec_driver_sql("SELECT sqlite_version()").scalar_one().split("."))
    return version >= (3, 43, 0)


def _has_table(conn: Connection, name: str) -> bool:
    return conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,)
    ).first() is not None


def candidate_messages(conn: Connection, query: str) -> Subquery | None:
    """Return candidate ledger rows, or None for the original LIKE scan.

    MATCH uses quoted *individual trigrams*, never user query syntax. detail=none
    permits only three-character tokens. The original escaped ILIKE remains the
    final authority for adjacency, punctuation, Unicode and literal wildcards.
    """
    if len(query) < 3 or "\x00" in query or not _available(conn):
        return None
    if not _has_table(conn, "message_search_fts"):
        return None
    ready = conn.exec_driver_sql("SELECT ready FROM message_search_state WHERE id = 1").scalar()
    if ready != 1:
        return None
    normalized = query.translate(_ASCII_LOWER)
    # A bounded subset suffices: these are necessary, not sufficient, matches.
    offsets = range(0, len(normalized) - 2, max(1, (len(normalized) - 2) // 8))
    terms = list(dict.fromkeys(normalized[i:i + 3] for i in offsets))[:8]
    expression = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
    # A runtime without FTS5 may open a database indexed by a different binary.
    # Probe the module without reading any documents before constructing MATCH.
    try:
        conn.exec_driver_sql("SELECT rowid FROM message_search_fts LIMIT 0")
    except OperationalError as exc:
        if _unsupported(exc):
            return None
        raise
    from storage.models import messages

    # CROSS JOIN is intentional: without it SQLite can scan the entire platform
    # index and probe candidates per row, even with a tiny FTS result. Flattening
    # this subquery preserves that join-order fence. Only outer-selected columns
    # are read (not content_json), and every remaining filter stays outside.
    return text("""
        SELECT messages.* FROM (
            SELECT d.message_id FROM message_search_fts
            JOIN message_search_documents AS d ON d.id = message_search_fts.rowid
            WHERE message_search_fts MATCH :message_search_match
            UNION SELECT message_id FROM message_search_dirty
            UNION SELECT message_id FROM message_search_documents WHERE oversized = 1
        ) AS candidates
        CROSS JOIN messages ON messages.id = candidates.message_id
    """).bindparams(message_search_match=expression).columns(*messages.c).subquery("search_messages")


def _unsupported(exc: OperationalError) -> bool:
    return any(fragment in str(exc.orig).lower() for fragment in (
        "no such module: fts5", "no such tokenizer: trigram", "unrecognized option: \"contentless_delete\"",
    ))


def _index_message(conn: Connection, message_id: str, content_text: str | None) -> None:
    if content_text is not None:
        conn.exec_driver_sql(
            "INSERT OR IGNORE INTO message_search_documents (message_id) VALUES (?)", (message_id,)
        )
    docid = conn.exec_driver_sql(
        "SELECT id FROM message_search_documents WHERE message_id = ?", (message_id,)
    ).scalar()
    if docid is not None:
        conn.exec_driver_sql("DELETE FROM message_search_fts WHERE rowid = ?", (docid,))
        if content_text is None:
            conn.exec_driver_sql("DELETE FROM message_search_documents WHERE id = ?", (docid,))
        else:
            oversized = len(content_text) > MAX_INDEX_CHARACTERS
            conn.exec_driver_sql(
                "UPDATE message_search_documents SET oversized = ? WHERE id = ?", (int(oversized), docid)
            )
            if not oversized:
                conn.exec_driver_sql(
                    "INSERT INTO message_search_fts (rowid, content_text) VALUES (?, ?)", (docid, content_text)
                )
    conn.exec_driver_sql("DELETE FROM message_search_dirty WHERE message_id = ?", (message_id,))


def _disable_index(conn: Connection) -> str:
    """Stop journaling when this runtime cannot index; retire old backlog boundedly.

    Removing the activation row makes triggers inert. A future capable runtime
    starts a full backfill, never trusts the stale derived cache, and reactivates
    journaling in the same transaction as its first indexed batch.
    """
    conn.exec_driver_sql("DELETE FROM message_search_state WHERE id = 1")
    conn.exec_driver_sql("""
        DELETE FROM message_search_dirty WHERE message_id IN (
            SELECT message_id FROM message_search_dirty ORDER BY message_id LIMIT ?
        )
    """, (BATCH_SIZE,))
    return "pending" if conn.exec_driver_sql("SELECT 1 FROM message_search_dirty LIMIT 1").first() else "unsupported"


def run_batch(engine: Engine, *, cancel_event: Event | None = None) -> str:
    """Commit at most one small batch; return pending/idle/cancelled/unsupported.

    The cursor, index and journal acknowledgements share one SQLite transaction.
    A crash before commit repeats the batch; after commit it resumes at the next
    key. BEGIN IMMEDIATE serializes maintenance workers with ordinary writers,
    so no write between reading content and acknowledging its id can be lost.
    """
    if cancel_event is not None and cancel_event.is_set():
        return "cancelled"
    with engine.connect() as conn:
        if not _has_table(conn, "message_search_state"):
            return "unsupported"
        supported = _available(conn)
        exists = supported and _has_table(conn, "message_search_fts")
        state = conn.exec_driver_sql("SELECT cursor, ready FROM message_search_state WHERE id = 1").first()
        if exists and state is not None and state.ready == 1:
            if conn.exec_driver_sql("SELECT 1 FROM message_search_dirty LIMIT 1").first() is None:
                return "idle"
        conn.rollback()
        old_timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        conn.exec_driver_sql("PRAGMA busy_timeout = 50")
        conn.commit()
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            if not supported:
                status = _disable_index(conn)
                conn.commit()
                return status
            # Re-read after acquiring the lock: a second process may have worked
            # while this worker was waiting. No process-local readiness cache.
            if not _has_table(conn, "message_search_fts"):
                conn.exec_driver_sql(_FTS_DDL)
                conn.exec_driver_sql("DELETE FROM message_search_state WHERE id = 1")
            conn.exec_driver_sql("INSERT OR IGNORE INTO message_search_state (id) VALUES (1)")
            state = conn.exec_driver_sql("SELECT cursor, ready FROM message_search_state WHERE id = 1").one()
            if state.ready != 1:
                if state.cursor is None:
                    rows = conn.exec_driver_sql(
                        f"SELECT id, {_TEXT_PREFIX} FROM messages ORDER BY id LIMIT ?", (BATCH_SIZE,)
                    ).all()
                else:
                    rows = conn.exec_driver_sql(
                        f"SELECT id, {_TEXT_PREFIX} FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                        (state.cursor, BATCH_SIZE),
                    ).all()
                for message_id, content in rows:
                    _index_message(conn, message_id, content)
                conn.exec_driver_sql(
                    "UPDATE message_search_state SET cursor = ?, ready = ? WHERE id = 1",
                    (rows[-1][0] if rows else state.cursor, int(len(rows) < BATCH_SIZE)),
                )
            else:
                rows = conn.exec_driver_sql(f"""
                    SELECT q.message_id, lower(substr(m.content_text, 1, {MAX_INDEX_CHARACTERS + 1}))
                    FROM message_search_dirty AS q
                    LEFT JOIN messages AS m ON m.id = q.message_id
                    ORDER BY q.message_id LIMIT ?
                """, (BATCH_SIZE,)).all()
                for message_id, content in rows:
                    _index_message(conn, message_id, content)
            if cancel_event is not None and cancel_event.is_set():
                conn.rollback()
                return "cancelled"
            conn.commit()
            return "pending"
        except OperationalError as exc:
            conn.rollback()
            if _unsupported(exc):
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                status = _disable_index(conn)
                conn.commit()
                return status
            if _is_sqlite_busy(exc):
                return "pending"
            raise
        finally:
            conn.rollback()
            conn.exec_driver_sql(f"PRAGMA busy_timeout = {int(old_timeout)}")
            conn.commit()


def run_pass(engine: Engine, *, cancel_event: Event | None = None) -> str:
    """Background maintenance only: bounded work, with writer opportunities between batches."""
    deadline = time.monotonic() + PASS_SECONDS
    while True:
        status = run_batch(engine, cancel_event=cancel_event)
        if status != "pending" or time.monotonic() >= deadline:
            return status
        if cancel_event is not None:
            if cancel_event.wait(0.01):
                return "cancelled"
        else:
            time.sleep(0.01)
