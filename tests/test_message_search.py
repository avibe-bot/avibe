"""Tests for message-content search (``search_messages``) and the ``around_id``
window mode on ``list_session_messages`` — Phase 1 of the message-search feature.

Mirrors the fixture pattern in ``tests/test_messages_service.py``: an isolated
``AVIBE_HOME`` (so the live ``~/.avibe`` is never touched), a SQLite engine
from ``create_sqlite_engine``, and direct inserts that control ``created_at`` /
``type`` / ``platform`` / ``content_text``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import messages_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, messages, scope_settings, scopes
from storage.settings_service import upsert_scope


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    yield tmp_path


def _seed_scope(conn, native_id: str = "proj_test", display_name: str = "My Project") -> str:
    now = messages_service._utc_now_iso()
    scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id=native_id, now=now)
    conn.execute(scopes.update().where(scopes.c.id == scope_id).values(display_name=display_name))
    return scope_id


def _seed_session(conn, scope_id, session_id, title=None, *, status="active") -> None:
    now = messages_service._utc_now_iso()
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            scope_id=scope_id,
            agent_backend="claude",
            agent_variant="default",
            session_anchor="anchor_" + session_id,
            native_session_id="",
            title=title,
            status=status,
            metadata_json="{}",
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
    )


def _insert_msg(
    conn,
    scope_id,
    session_id,
    author,
    text,
    created_at,
    *,
    platform="avibe",
    msg_type=None,
    source=None,
    msg_id=None,
):
    """Direct insert so a test controls created_at / type / platform / text.

    Agent rows default to ``result``, human rows to ``user`` — the two
    transcript-visible types message search scans. ``text=None`` writes a NULL
    ``content_text``."""
    resolved_type = msg_type or ("user" if author == "user" else "result")
    conn.execute(
        messages.insert().values(
            id=msg_id or f"msg_{session_id}_{created_at[-9:]}_{author}_{resolved_type}",
            scope_id=scope_id,
            session_id=session_id,
            platform=platform,
            author=author,
            type=resolved_type,
            source=source,
            content_text=text,
            content_json="{}",
            metadata_json="{}",
            created_at=created_at,
            updated_at=created_at,
            read_at=None,
        )
    )


# --- search_messages --------------------------------------------------------


def test_search_finds_latin_term_with_session_grouping_and_snippet(isolated_state):
    """A Latin term is found across sessions, grouped by session (most-recent
    match first), newest match first within a session, with a split snippet."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_a", "Alpha")
        _seed_session(conn, scope_id, "ses_b", "Beta")
        # ses_a: two matches (older + newer) + a non-match.
        _insert_msg(conn, scope_id, "ses_a", "user", "deploy the staging server", "2026-06-01T10:00:00Z")
        _insert_msg(conn, scope_id, "ses_a", "agent", "unrelated reply", "2026-06-01T10:01:00Z")
        _insert_msg(conn, scope_id, "ses_a", "agent", "I will DEPLOY it now", "2026-06-01T10:02:00Z")
        # ses_b: a single, most-recent match → ranks first.
        _insert_msg(conn, scope_id, "ses_b", "user", "can we deploy on friday", "2026-06-01T11:00:00Z")

    with engine.connect() as conn:
        result = messages_service.search_messages(conn, query="deploy")

    assert result["total"] == 3
    assert result["session_count"] == 2
    # ses_b's match (11:00) is newer than any ses_a match → ses_b first.
    assert [s["session_id"] for s in result["sessions"]] == ["ses_b", "ses_a"]

    sb, sa = result["sessions"]
    assert sb["title"] == "Beta" and sb["project_id"] == "proj_test" and sb["project_name"] == "My Project"
    # Within ses_a: newest match first.
    assert [m["created_at"] for m in sa["matches"]] == ["2026-06-01T10:02:00Z", "2026-06-01T10:00:00Z"]

    # Snippet shape: three string fields; match carries ORIGINAL casing.
    newest = sa["matches"][0]
    assert set(newest["snippet"]) == {"prefix", "match", "suffix"}
    assert newest["snippet"]["match"] == "DEPLOY"  # original case preserved
    # Each match exposes the row identity needed to deep-link + render.
    assert set(newest) == {"id", "author", "source", "type", "created_at", "snippet"}


def test_search_finds_cjk_term(isolated_state):
    """Substring search is bilingual — a CJK term matches (LIKE, no tokenizer)."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_cjk", "中文会话")
        _insert_msg(conn, scope_id, "ses_cjk", "user", "请帮我部署测试服务器", "2026-06-01T10:00:00Z")
        _insert_msg(conn, scope_id, "ses_cjk", "agent", "好的，正在处理", "2026-06-01T10:01:00Z")

    with engine.connect() as conn:
        result = messages_service.search_messages(conn, query="部署")

    assert result["total"] == 1
    match = result["sessions"][0]["matches"][0]
    assert match["snippet"]["match"] == "部署"


def test_search_finds_harness_prompt_by_default(isolated_state):
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_harness", "Automated")
        _insert_msg(
            conn,
            scope_id,
            "ses_harness",
            "harness",
            "inspect callback delivery",
            "2026-06-01T10:00:00Z",
            msg_type=messages_service.HARNESS_TYPE,
            source="harness",
        )

    with engine.connect() as conn:
        result = messages_service.search_messages(conn, query="callback")

    match = result["sessions"][0]["matches"][0]
    assert (match["author"], match["source"], match["type"]) == (
        "harness",
        "harness",
        messages_service.HARNESS_TYPE,
    )


def test_search_excludes_im_platform_rows(isolated_state):
    """A ``result`` row on a non-avibe (IM) platform is excluded for the default
    platform='avibe' search — search is Workbench-scoped."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_mix", "Mixed")
        _insert_msg(conn, scope_id, "ses_mix", "agent", "deploy from avibe", "2026-06-01T10:00:00Z", platform="avibe")
        _insert_msg(conn, scope_id, "ses_mix", "agent", "deploy from slack", "2026-06-01T10:01:00Z", platform="slack")

    with engine.connect() as conn:
        result = messages_service.search_messages(conn, query="deploy")

    assert result["total"] == 1
    assert result["sessions"][0]["matches"][0]["snippet"]["match"] == "deploy"
    # The slack row's text must not appear.
    texts = [m["snippet"] for s in result["sessions"] for m in s["matches"]]
    assert all("slack" not in (sn["prefix"] + sn["suffix"]) for sn in texts)


def test_search_excludes_assistant_and_null_content(isolated_state):
    """Intermediate ``assistant`` process rows and NULL ``content_text`` rows are
    excluded — only user/result rows with text are searchable."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_t", "Types")
        # assistant (intermediate) — excluded even though it matches.
        _insert_msg(conn, scope_id, "ses_t", "agent", "deploy thinking", "2026-06-01T10:00:00Z", msg_type="assistant")
        # NULL content_text — excluded (media-only row).
        _insert_msg(conn, scope_id, "ses_t", "agent", None, "2026-06-01T10:01:00Z", msg_type="result")
        # A real user/result match — kept.
        _insert_msg(conn, scope_id, "ses_t", "user", "deploy please", "2026-06-01T10:02:00Z")

    with engine.connect() as conn:
        result = messages_service.search_messages(conn, query="deploy")

    assert result["total"] == 1
    assert result["sessions"][0]["matches"][0]["type"] == "user"


def test_search_excludes_archived_session(isolated_state):
    """Messages of an archived (soft-deleted) session never surface in search."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_live", "Live", status="active")
        _seed_session(conn, scope_id, "ses_arch", "Archived", status="archived")
        _insert_msg(conn, scope_id, "ses_live", "user", "deploy here", "2026-06-01T10:00:00Z")
        _insert_msg(conn, scope_id, "ses_arch", "user", "deploy there", "2026-06-01T10:01:00Z")

    with engine.connect() as conn:
        result = messages_service.search_messages(conn, query="deploy")

    assert result["session_count"] == 1
    assert result["sessions"][0]["session_id"] == "ses_live"


def test_search_excludes_archived_project_scope(isolated_state):
    """A message in an ACTIVE session under an archived PROJECT is excluded.

    ``projects_service.archive_project`` archives a project by setting
    ``scope_settings.enabled = 0`` while leaving its sessions ``active``, so the
    disabled scope — not a session status — is the "archived project" signal.
    A scope with no ``scope_settings`` row is still treated as enabled."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        # Live project (no scope_settings row → treated as enabled) keeps its match.
        live_scope = _seed_scope(conn, native_id="proj_live", display_name="Live Project")
        _seed_session(conn, live_scope, "ses_live", "Live", status="active")
        _insert_msg(conn, live_scope, "ses_live", "user", "deploy here", "2026-06-01T10:00:00Z")
        # Archived project: an explicit scope_settings row with enabled=0, but its
        # session stays active — its messages must NOT surface.
        arch_scope = _seed_scope(conn, native_id="proj_arch", display_name="Archived Project")
        _seed_session(conn, arch_scope, "ses_arch_proj", "Under Archived", status="active")
        _insert_msg(conn, arch_scope, "ses_arch_proj", "user", "deploy there", "2026-06-01T10:01:00Z")
        now = messages_service._utc_now_iso()
        conn.execute(
            scope_settings.insert().values(
                scope_id=arch_scope,
                enabled=0,
                settings_version=1,
                settings_json="{}",
                created_at=now,
                updated_at=now,
            )
        )

    with engine.connect() as conn:
        result = messages_service.search_messages(conn, query="deploy")

    assert result["session_count"] == 1
    assert result["sessions"][0]["session_id"] == "ses_live"


def test_search_treats_like_metachars_literally(isolated_state):
    """``%`` / ``_`` in the query are matched literally (escaped), not as LIKE
    wildcards — a ``%`` query must not match a row lacking a literal ``%``."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_pct", "Percent")
        _insert_msg(conn, scope_id, "ses_pct", "user", "cpu at 50% load", "2026-06-01T10:00:00Z")
        _insert_msg(conn, scope_id, "ses_pct", "user", "plain text no metachars", "2026-06-01T10:01:00Z")
        _insert_msg(conn, scope_id, "ses_pct", "user", "value_x set", "2026-06-01T10:02:00Z")
        _insert_msg(conn, scope_id, "ses_pct", "user", "valueXset other", "2026-06-01T10:03:00Z")

    with engine.connect() as conn:
        pct = messages_service.search_messages(conn, query="50%")
        underscore = messages_service.search_messages(conn, query="value_x")

    # '%' is literal: only the "50% load" row matches (not every row via wildcard).
    assert pct["total"] == 1
    assert pct["sessions"][0]["matches"][0]["snippet"]["match"] == "50%"
    # '_' is literal: matches "value_x", not "valueXset".
    assert underscore["total"] == 1
    assert underscore["sessions"][0]["matches"][0]["snippet"]["match"] == "value_x"


def test_search_empty_or_whitespace_query_is_empty(isolated_state):
    """An empty / whitespace-only query short-circuits to an empty result."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_e", "Empty")
        _insert_msg(conn, scope_id, "ses_e", "user", "anything", "2026-06-01T10:00:00Z")

    empty_shape = {"sessions": [], "total": 0, "session_count": 0}
    with engine.connect() as conn:
        assert messages_service.search_messages(conn, query="") == empty_shape
        assert messages_service.search_messages(conn, query="   \n\t ") == empty_shape


def test_search_snippet_windows_and_truncates(isolated_state):
    """The snippet keeps ~40 chars before / ~50 after the match, collapses
    whitespace+newlines to single spaces, and marks truncated ends with '…'."""
    engine = create_sqlite_engine()
    long_before = "x" * 100
    long_after = "y" * 100
    text = f"{long_before}\n\nNEEDLE\t  here  {long_after}"
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_snip", "Snip")
        _insert_msg(conn, scope_id, "ses_snip", "user", text, "2026-06-01T10:00:00Z")
        # Fallback case: query matches the title's column path but NOT content_text
        # — we still get a head-of-text snippet with an empty match.
        _insert_msg(conn, scope_id, "ses_snip", "user", "short body", "2026-06-01T10:01:00Z", msg_id="fallback_row")

    with engine.connect() as conn:
        result = messages_service.search_messages(conn, query="needle")

    match = result["sessions"][0]["matches"][0]["snippet"]
    assert match["match"] == "NEEDLE"  # original case
    # Prefix is truncated at the start (… leading) and windowed to ~40 chars.
    assert match["prefix"].startswith("…")
    assert len(match["prefix"]) <= 45
    # Suffix truncated at the end (… trailing), whitespace collapsed (no tabs/nl).
    assert match["suffix"].endswith("…")
    assert "\n" not in match["prefix"] and "\t" not in match["suffix"]
    # Whitespace right after the match was collapsed to a single space.
    assert match["suffix"].startswith(" here ") or match["suffix"].startswith("here ")


def test_search_snippet_fallback_when_term_not_in_content(isolated_state):
    """If the matched substring can't be located (defensive: e.g. odd casing /
    normalization), the snippet falls back to a head-of-text prefix + empty match.
    Verified directly on the builder to exercise the not-found branch."""
    snippet = messages_service.build_snippet("a" * 200, "zzz-not-present")
    assert snippet["match"] == ""
    assert snippet["suffix"] == ""
    assert snippet["prefix"].endswith("…")
    assert len(snippet["prefix"]) <= 91  # ~90 head + the ellipsis


def test_search_limit_clamped_and_caps_matches(isolated_state):
    """``limit`` caps the matched-message scan (newest first) and is clamped to
    1..200."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_session(conn, scope_id, "ses_lim", "Limit")
        for i in range(5):
            _insert_msg(conn, scope_id, "ses_lim", "user", f"deploy {i}", f"2026-06-01T10:0{i}:00Z")

    with engine.connect() as conn:
        capped = messages_service.search_messages(conn, query="deploy", limit=2)
        # limit<1 clamps up to 1.
        floored = messages_service.search_messages(conn, query="deploy", limit=0)

    assert capped["total"] == 2
    # Newest first: deploy 4 then deploy 3.
    assert [m["snippet"]["match"] for m in capped["sessions"][0]["matches"]] == ["deploy", "deploy"]
    assert [m["created_at"] for m in capped["sessions"][0]["matches"]] == [
        "2026-06-01T10:04:00Z",
        "2026-06-01T10:03:00Z",
    ]
    assert floored["total"] == 1


# --- list_session_messages around_id window ---------------------------------


def _seed_linear_session(conn, scope_id, session_id, count):
    """Insert ``count`` user messages m0..m{count-1} at increasing timestamps."""
    _seed_session(conn, scope_id, session_id)
    for i in range(count):
        _insert_msg(conn, scope_id, session_id, "user", f"m{i}", f"2026-06-02T10:{i:02d}:00Z")


def test_around_middle_target_returns_window_with_both_cursors(isolated_state):
    """A middle anchor yields up to ``limit`` older + anchor + ``limit`` newer,
    merged chronologically, with both cursors set when rows remain on each side."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_linear_session(conn, scope_id, "ses_mid", 9)  # m0..m8

    with engine.connect() as conn:
        anchor_id = f"msg_ses_mid_{'2026-06-02T10:04:00Z'[-9:]}_user_user"
        page = messages_service.list_session_messages(
            conn, session_id="ses_mid", around_id=anchor_id, limit=2, types=("user", "result")
        )

    # 2 older (m2, m3) + anchor (m4) + 2 newer (m5, m6), chronological.
    assert [m["text"] for m in page["messages"]] == ["m2", "m3", "m4", "m5", "m6"]
    # Older rows remain (m0, m1) → before cursor; newer remain (m7, m8) → after cursor.
    assert page["next_before_id"] is not None
    assert page["next_after_id"] is not None


def test_around_newest_target_has_no_after_cursor(isolated_state):
    """When the anchor is the newest message, the window has older rows but no
    newer ones → ``next_after_id`` is None."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_linear_session(conn, scope_id, "ses_new", 5)  # m0..m4

    with engine.connect() as conn:
        anchor_id = f"msg_ses_new_{'2026-06-02T10:04:00Z'[-9:]}_user_user"  # m4 = newest
        page = messages_service.list_session_messages(
            conn, session_id="ses_new", around_id=anchor_id, limit=2, types=("user", "result")
        )

    # m2, m3 older + m4 anchor; nothing newer.
    assert [m["text"] for m in page["messages"]] == ["m2", "m3", "m4"]
    assert page["next_after_id"] is None
    assert page["next_before_id"] is not None  # m0, m1 still older


def test_around_oldest_target_has_no_before_cursor(isolated_state):
    """When the anchor is the oldest message, there are no older rows →
    ``next_before_id`` is None."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_linear_session(conn, scope_id, "ses_old", 5)  # m0..m4

    with engine.connect() as conn:
        anchor_id = f"msg_ses_old_{'2026-06-02T10:00:00Z'[-9:]}_user_user"  # m0 = oldest
        page = messages_service.list_session_messages(
            conn, session_id="ses_old", around_id=anchor_id, limit=2, types=("user", "result")
        )

    # anchor m0 + m1, m2 newer; nothing older.
    assert [m["text"] for m in page["messages"]] == ["m0", "m1", "m2"]
    assert page["next_before_id"] is None
    assert page["next_after_id"] is not None  # m3, m4 still newer


def test_around_unknown_id_returns_empty(isolated_state):
    """An ``around_id`` that doesn't exist (or belongs to another session) returns
    no messages and null cursors."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_linear_session(conn, scope_id, "ses_unk", 3)

    with engine.connect() as conn:
        page = messages_service.list_session_messages(
            conn, session_id="ses_unk", around_id="does-not-exist", limit=2, types=("user", "result")
        )

    assert page == {"messages": [], "next_after_id": None, "next_before_id": None}


def test_around_takes_precedence_over_before_and_tail(isolated_state):
    """``around_id`` wins over ``before_id`` / ``tail`` when several are passed."""
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = _seed_scope(conn)
        _seed_linear_session(conn, scope_id, "ses_prec", 7)  # m0..m6

    with engine.connect() as conn:
        anchor_id = f"msg_ses_prec_{'2026-06-02T10:03:00Z'[-9:]}_user_user"  # m3
        page = messages_service.list_session_messages(
            conn,
            session_id="ses_prec",
            around_id=anchor_id,
            before_id=anchor_id,
            tail=True,
            limit=1,
            types=("user", "result"),
        )

    # Around window centered on m3 (1 older + anchor + 1 newer), not a tail/before page.
    assert [m["text"] for m in page["messages"]] == ["m2", "m3", "m4"]
