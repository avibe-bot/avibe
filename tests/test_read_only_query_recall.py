"""Recall through ``vibe data query``: the history views and the long-text functions.

Agents recall earlier work by querying Avibe's own state. They kept guessing a
``sessions`` table and a ``role`` column, filtered on a ``type='agent'`` that does
not exist (an empty answer, not an error), and bounded long text with
``substr(text, 1, n)``, which keeps the opening and loses the match.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from config import paths
from storage.background import SQLiteBackgroundTaskStore
from storage.pagination import PageRequest
from storage.read_only_query import RECALL_VIEWS, ReadOnlyQueryError, run_read_only_query

PARENT = "ses-parent"
CHILD = "ses-child"
OTHER = "ses-other"
TURN = "trn-child-1"


@pytest.fixture
def state_db(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    SQLiteBackgroundTaskStore().close()  # the migrated schema
    return paths.get_sqlite_state_path()


def _session(conn, session_id: str, title: str, created_at: str) -> None:
    conn.execute(
        "insert into agent_sessions (id, agent_backend, agent_variant, session_anchor, native_session_id, status,"
        " metadata_json, created_at, updated_at, last_active_at, title, agent_name)"
        " values (?, 'claude', 'default', ?, '', 'active', '{}', ?, ?, ?, ?, 'claude')",
        (session_id, session_id, created_at, created_at, created_at, title),
    )


def _run(conn, run_id: str, session_id: str, policy: str, source_actor: str, created_at: str) -> None:
    conn.execute(
        "insert into agent_runs (id, run_type, status, session_id, session_policy, source_kind, source_actor,"
        " message, metadata_json, created_at, updated_at)"
        " values (?, 'agent_run', 'succeeded', ?, ?, 'agent', ?, 'work', '{}', ?, ?)",
        (run_id, session_id, policy, source_actor, created_at, created_at),
    )


def _message(conn, message_id: str, session_id: str, author: str, kind: str, text: str, at: str, turn: str | None = None):
    conn.execute(
        "insert into messages (id, session_id, platform, author, type, content_text, content_json, metadata_json,"
        " created_at, updated_at) values (?, ?, 'avibe', ?, ?, ?, '{}', ?, ?, ?)",
        (message_id, session_id, author, kind, text, json.dumps({"turn_id": turn} if turn else {}), at, at),
    )


def _seed_history(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        _session(conn, PARENT, "Release review", "2026-10-01T00:00:00Z")
        _session(conn, CHILD, "Lane A", "2026-10-01T01:00:00Z")
        _session(conn, OTHER, "Unrelated", "2026-10-01T02:00:00Z")
        # The parent's Agent created the child; a later Agent only continued the parent.
        _run(conn, "run-create", CHILD, "none", PARENT, "2026-10-01T01:00:00Z")
        _run(conn, "run-continue", PARENT, "existing", OTHER, "2026-10-01T03:00:00Z")
        conn.execute(
            "insert into session_turns (id, session_id, initial_delivery_id, state, backend, start_attempt_id,"
            " start_receipt_outcome, dispatch_text, dispatch_sha256, terminal_outcome, created_at, updated_at,"
            " started_at, terminal_at) values (?, ?, 'dlv-1', 'terminal', 'claude', 'atm-1', 'accepted',"
            " 'Slim the watch list rows', 'sha', 'completed', ?, ?, ?, ?)",
            (TURN, CHILD, "2026-10-01T01:00:00Z", "2026-10-01T01:30:00Z", "2026-10-01T01:00:00Z", "2026-10-01T01:30:00Z"),
        )
        _message(conn, "m1", CHILD, "harness", "harness", "Slim the watch list rows", "2026-10-01T01:00:01Z")
        _message(conn, "m2", CHILD, "agent", "assistant", "", "2026-10-01T01:00:02Z", TURN)
        _message(conn, "m3", CHILD, "agent", "assistant", "Reading the projection", "2026-10-01T01:00:03Z", TURN)
        _message(conn, "m4", CHILD, "agent", "result", "First reply", "2026-10-01T01:10:00Z", TURN)
        _message(conn, "m5", CHILD, "agent", "result", "PR #2971 is open", "2026-10-01T01:29:00Z", TURN)
        _message(conn, "m6", PARENT, "user", "user", "Ship it", "2026-10-01T00:00:01Z")
        conn.commit()


def _rows(sql: str, db_path: Path) -> list[dict]:
    return run_read_only_query(sql, db_path=db_path, page_request=PageRequest(page=1, limit=100)).rows


def test_history_views_answer_in_the_shape_agents_ask_for(state_db: Path) -> None:
    _seed_history(state_db)

    sessions = {
        row["session_id"]: row
        for row in _rows("select session_id, title, parent_session_id from sessions", state_db)
    }
    assert sessions[CHILD]["parent_session_id"] == PARENT
    # Being continued by another Agent does not make it the parent.
    assert sessions[PARENT]["parent_session_id"] is None

    transcript = _rows(
        f"select role, kind, text, turn_id from transcript where session_id = '{CHILD}' order by at", state_db
    )
    assert [(row["role"], row["kind"], row["text"]) for row in transcript] == [
        ("harness", "harness", "Slim the watch list rows"),
        ("agent", "assistant", "Reading the projection"),
        ("agent", "result", "First reply"),
        ("agent", "result", "PR #2971 is open"),
    ]
    assert transcript[-1]["turn_id"] == TURN

    (turn,) = _rows(f"select turn_id, outcome, input, reply from turns where session_id = '{CHILD}'", state_db)
    assert turn == {
        "turn_id": TURN,
        "outcome": "completed",
        "input": "Slim the watch list rows",
        "reply": "PR #2971 is open",
    }


def test_history_views_read_through_indexes(state_db: Path) -> None:
    """A Session's transcript and each Turn's reply are index lookups, not scans of every message.

    ``turns`` matches the indexed turn-id expression against ``+t.id``: compared with a
    TEXT column, the expression would take its affinity and the index would only scan.
    """

    def plan(sql: str) -> list[str]:
        return [row["detail"] for row in _rows(f"explain query plan {sql}", state_db)]

    transcript = plan("select * from transcript where session_id = 'x' order by at")
    assert any("USING INDEX ix_messages_session_transcript_id" in step for step in transcript), transcript

    turns = plan("select reply from turns where session_id = 'x'")
    assert any(step.startswith("SEARCH") and "ix_messages_turn_id" in step for step in turns), turns
    assert not any(step.startswith("SCAN") and "messages" in step for step in turns), turns


def test_history_views_never_hide_a_real_table(state_db: Path) -> None:
    with sqlite3.connect(state_db) as conn:
        tables = {name for (name,) in conn.execute("select name from sqlite_master")}
    assert not set(RECALL_VIEWS) & tables


def test_excerpt_shows_the_match_not_the_opening(state_db: Path) -> None:
    text = "intro " * 200 + "the Watch List rows were slimmed" + " outro" * 200
    sql = (
        f"select excerpt('{text}', 'watch list', 40) as hit, excerpt('{text}', 'absent', 20) as miss,"
        " excerpt('short', 'x') as short, excerpt(null, 'x') as gone"
    )

    (row,) = _rows(sql, state_db)

    assert row["hit"].startswith("…") and row["hit"].endswith("…")
    assert "Watch List" in row["hit"] and len(row["hit"]) == 42
    assert row["miss"] == "intro intro intro in…"
    assert row["short"] == "short"
    assert row["gone"] is None


def test_regexp_matches_python_patterns_and_explains_a_bad_one(state_db: Path) -> None:
    _seed_history(state_db)

    hits = _rows("select message_id from transcript where text regexp '(?i)pr ?#?2971\\b'", state_db)
    assert [row["message_id"] for row in hits] == ["m5"]

    with pytest.raises(ReadOnlyQueryError) as exc_info:
        _rows("select 1 from transcript where text regexp 'pr ('", state_db)
    assert str(exc_info.value).startswith("regexp: invalid regular expression 'pr ('")
    assert "Python regular expression" in exc_info.value.hint


def test_unknown_names_point_at_the_views(state_db: Path) -> None:
    with pytest.raises(ReadOnlyQueryError) as exc_info:
        _rows("select * from session", state_db)
    assert "Did you mean sessions" in exc_info.value.hint

    with pytest.raises(ReadOnlyQueryError) as exc_info:
        _rows("select role from messages", state_db)
    assert "'role' is a column of transcript" in exc_info.value.hint
