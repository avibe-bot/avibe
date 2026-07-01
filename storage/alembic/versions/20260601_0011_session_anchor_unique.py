"""pin sessions to (scope, anchor): strip cwd from anchors, dedup, unique index

Realises the new session model where a thread is one session pinned to one
backend, keyed by ``(scope_id, session_anchor)`` (see
docs/plans/session-anchor-backend-pin.md):

1. OpenCode used to fold the working directory into ``session_anchor``
   (``base:/path``) to rotate a session per cwd. Current runtime never trusts
   that suffix as cwd, so strip the ``:<cwd>`` suffix back to the bare base
   anchor without deriving ``workdir`` from it. Only ABSOLUTE-path suffixes are
   stripped, so claude/codex ``base:<subagent>`` anchors are preserved.
2. With anchors normalised, an IM thread that toggled backends (or had a cwd
   suffix) can have several rows for one ``(scope_id, session_anchor)``. Reattach
   the loser rows' transcripts to the survivor, then keep the most-recently-active
   row per group and delete the rest — a thread is now ONE session.
3. Add a UNIQUE index on ``(scope_id, session_anchor)`` so the invariant holds
   going forward.

NOTE: steps 1-2 rewrite/remove rows and are NOT reversible; ``downgrade`` only
drops the unique index.

Revision ID: 20260601_0011
Revises: 20260531_0010
Create Date: 2026-06-01
"""

from __future__ import annotations

import re

from alembic import op

revision = "20260601_0011"
down_revision = "20260531_0010"
branch_labels = None
depends_on = None

_UNIQUE_INDEX = "uq_agent_sessions_scope_anchor"

# An ABSOLUTE cwd suffix: POSIX ``/...``, Windows drive ``C:\`` / ``C:/``, or UNC
# ``\\...``. Mirrors storage.sessions_service._ABS_CWD_PREFIX (kept inline so the
# migration stays self-contained / independent of app-code drift).
_ABS_CWD_PREFIX = re.compile(r"(/|[A-Za-z]:[\\/]|\\\\)")


def _strip_anchor_cwd(anchor: str) -> str:
    """Strip ``base:<abs-cwd>`` to ``bare_base``; passthrough otherwise.

    Splits on the FIRST ``:`` and only strips when the suffix is an absolute path,
    so an OpenCode cwd composite (POSIX/Windows/UNC, even double-nested
    ``base:/p:/p``) collapses to the bare base while a claude/codex subagent name
    (``base:reviewer``) is preserved. First-colon also tolerates the Windows
    drive-letter colon that a last-colon split would mangle into ``base:C``."""
    base, sep, suffix = anchor.partition(":")
    if sep and base and _ABS_CWD_PREFIX.match(suffix):
        return base
    return anchor


def _tables(bind) -> set[str]:
    return {row[0] for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")}


# Window expression selecting, for every agent_sessions row, the id that survives
# dedup for its (scope_id, session_anchor) group: the most-recently-active row
# (``last_active_at`` desc, then ``id`` desc as a stable tiebreak). Reused by both
# the message-reattach and the delete below so they agree on the winner.
_DEDUP_ANCHORED = """
    select id,
           row_number() over (
               partition by scope_id, session_anchor
               order by coalesce(last_active_at, '') desc, id desc
           ) as rn,
           first_value(id) over (
               partition by scope_id, session_anchor
               order by coalesce(last_active_at, '') desc, id desc
           ) as keep_id
    from agent_sessions
"""


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "agent_sessions" not in tables:
        return

    # 1. Strip the ``:<cwd>`` suffix OpenCode folded into the anchor back to the
    #    bare base. Do not derive ``workdir`` from that suffix. Done row-by-row in
    #    Python (not SQL): only an ABSOLUTE-path suffix is a cwd, and recognising
    #    POSIX + Windows (``C:\``) + UNC absolute paths — and tolerating the Windows
    #    drive-letter colon — is not expressible in portable SQLite string ops. A
    #    claude/codex SUBAGENT anchor (``base:reviewer``) is NOT a path and is
    #    preserved; collapsing it would let the dedup below delete the subagent's
    #    native binding (lost context). avibe anchors (no ``:``) never match.
    rows = bind.exec_driver_sql("select id, session_anchor from agent_sessions").fetchall()
    for row_id, anchor in rows:
        if anchor is None:
            continue
        bare = _strip_anchor_cwd(str(anchor))
        if bare != anchor:
            bind.exec_driver_sql(
                "update agent_sessions set session_anchor = ? where id = ?",
                (bare, row_id),
            )

    # 2. Reattach transcripts before dedup deletes the loser rows. messages.session_id
    #    is FK ``ondelete=SET NULL`` and alembic runs with ``PRAGMA foreign_keys=ON``,
    #    so deleting a duplicate session row would orphan its messages and drop them
    #    from ``/api/sessions/<kept>/messages``. Point every loser row's messages at
    #    the row that survives dedup for the same (scope, anchor) first, so the
    #    consolidated session keeps the full visible history.
    if "messages" in tables:
        bind.exec_driver_sql(
            f"""
            update messages
            set session_id = (
                select w.keep_id from ({_DEDUP_ANCHORED}) as w
                where w.id = messages.session_id
            )
            where messages.session_id in (
                select id from ({_DEDUP_ANCHORED}) where rn > 1
            )
            """
        )

    # 3. Dedup: keep the most-recently-active row per (scope_id, session_anchor),
    #    delete the rest. A thread is now ONE session regardless of how many
    #    backends it cycled through. (Messages were reattached above.)
    bind.exec_driver_sql(
        f"""
        delete from agent_sessions
        where id in (
            select id from ({_DEDUP_ANCHORED}) where rn > 1
        )
        """
    )

    # 4. Enforce uniqueness going forward. (NULL scope_id rows stay distinct under
    #    SQLite's NULL semantics — they're orphaned/legacy and not load-bearing.)
    bind.exec_driver_sql(
        f"create unique index if not exists {_UNIQUE_INDEX} "
        "on agent_sessions (scope_id, session_anchor)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_sessions" in _tables(bind):
        bind.exec_driver_sql(f"drop index if exists {_UNIQUE_INDEX}")
