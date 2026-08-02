"""The reserved workspace-notifications Session is a SYSTEM surface, not an ordinary chat.

D5 rung (5) of the harness failure ladder needs one row that is TWO things at once: a
destination the runtime can always deliver a caller-less failure notice to, and NOT a
chat that shows up in a user's session lists (nothing can be usefully said to it — no
backend, no turns).

The two ``agent_sessions.visibility`` values that existed each gave exactly one half:

* ``foreground`` delivers, and is an ordinary session in every list.
* ``background`` hides, and is filtered OUT of ``list_inbox_sessions`` /
  ``unread_counts_by_session`` — so it hides the notice itself — and additionally sets
  ``suppress_delivery`` (``core/internal_server.py``, ``core/scheduled_tasks.py``), so
  no realtime ``inbox.session.updated`` and no Web Push either.

``visibility='system'`` is the projection that satisfies both, and this module pins each
half plus the refutation of the ``background`` alternative, so nobody "simplifies" the
fix back into the impasse. See
``storage.agent_session_rows.resolve_workspace_notice_session``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import paths
from storage import messages_service
from storage import workbench_sessions_service as wss
from storage.agent_session_rows import (
    ASSIGNABLE_SESSION_VISIBILITIES,
    INBOX_SESSION_VISIBILITIES,
    SESSION_VISIBILITIES,
    WORKSPACE_NOTICE_SESSION_ANCHOR,
    WORKSPACE_NOTICE_SESSION_ID,
    WORKSPACE_NOTICE_SESSION_VISIBILITY,
    resolve_workspace_notice_session,
)
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions
from storage.sessions_service import SQLiteSessionsService

# The notice rows the harness actually writes into this session are agent ``notify``
# messages: an inbox PREVIEW type (so they raise a card) but not an ``unread`` type (a
# failure notify is not an unread reply and must not bump the badge).
NOTICE_TYPE = "notify"


def _migrated_db(monkeypatch, tmp_path: Path) -> Path:
    """A real migrated state DB, not ``metadata.create_all``.

    The projection is enforced by indexed predicates over columns whose defaults live in
    the Alembic revisions, so the tests run against the schema an install actually has.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    return paths.get_sqlite_state_path()


def _reserved_with_one_notice(db_path: Path):
    """Create the reserved row and give it one notice, so it is inbox-eligible."""
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        session_id = resolve_workspace_notice_session(conn, title="Workspace notifications")
        messages_service.append(
            conn,
            scope_id=None,
            session_id=session_id,
            platform="avibe",
            author="agent",
            message_type=NOTICE_TYPE,
            text="Scheduled task 'nightly' failed: delivery target missing",
        )
    assert session_id == WORKSPACE_NOTICE_SESSION_ID
    return engine


def _ordinary_session(db_path: Path, *, channel: str) -> tuple[str, str]:
    service = SQLiteSessionsService(db_path)
    try:
        sid = service.bind_agent_session(
            scope_key=f"slack::channel::{channel}",
            agent_name="claude",
            session_anchor=f"slack_{channel}",
            native_session_id=f"native-{channel}",
        )
        assert sid is not None
        return sid, service.get_agent_session_by_id(sid)["scope_id"]
    finally:
        service.close()


def _list_ids(conn) -> list[str]:
    return [row["id"] for row in wss.list_sessions(conn, limit=100)["sessions"]]


def _paged_ids(conn) -> list[str]:
    return [row["id"] for row in wss.list_sessions_page(conn, limit=100).items]


def _inbox_ids(conn) -> list[str]:
    return [row["session_id"] for row in messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"]]


# ── the vocabulary itself ────────────────────────────────────────────────────


def test_system_is_a_storage_visibility_that_no_caller_can_assign() -> None:
    """Third value in the closed vocabulary, and NOT in the caller-assignable subset.

    Why a value in ``visibility`` at all: it is exactly the axis every session-list and
    inbox surface already filters on, and it is indexed
    (``ix_agent_sessions_visibility``, ``storage/models.py``), so the projection costs one
    predicate on queries that already carry one. A ``metadata_json`` flag would push JSON
    parsing into those hot queries; a dedicated column would be a migration for a single
    row. The split matters as much as the value: ``system`` classifies a row the RUNTIME
    owns, so a caller must not be able to label an ordinary chat with it.
    """
    assert WORKSPACE_NOTICE_SESSION_VISIBILITY == "system"
    assert SESSION_VISIBILITIES == {"foreground", "background", "system"}
    assert ASSIGNABLE_SESSION_VISIBILITIES == {"foreground", "background"}
    assert "system" not in ASSIGNABLE_SESSION_VISIBILITIES
    # Positive admission list: a future fourth visibility is hidden by default.
    assert set(INBOX_SESSION_VISIBILITIES) == {"foreground", "system"}


def test_the_reserved_row_is_created_and_healed_as_system(monkeypatch, tmp_path: Path) -> None:
    """Both write paths (create, heal) land on ``system``.

    The heal is also the migration: this row exists in no release, so a round-12/13
    development row created as ``foreground`` is converted by the next notice rather than
    by an Alembic revision.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        resolve_workspace_notice_session(conn, title="Workspace notifications")
    with engine.connect() as conn:
        row = conn.execute(
            select(agent_sessions.c.visibility, agent_sessions.c.agent_backend, agent_sessions.c.scope_id)
            .where(agent_sessions.c.id == WORKSPACE_NOTICE_SESSION_ID)
        ).mappings().one()
    assert row["visibility"] == "system"
    # No backend and no turn: nothing is ever dispatched into it.
    assert row["agent_backend"] == ""
    assert row["scope_id"] is None

    # A pre-``system`` row (exactly what a round-12/13 dev database holds) is repaired.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_sessions SET visibility = 'foreground' WHERE id = :sid"),
            {"sid": WORKSPACE_NOTICE_SESSION_ID},
        )
    with engine.begin() as conn:
        assert resolve_workspace_notice_session(conn) == WORKSPACE_NOTICE_SESSION_ID
    with engine.connect() as conn:
        assert _visibility(conn) == "system"
        # The heal repairs only what makes the row unusable — the title it was named
        # with is somebody's record.
        title = conn.execute(
            select(agent_sessions.c.title).where(agent_sessions.c.id == WORKSPACE_NOTICE_SESSION_ID)
        ).scalar_one()
    assert title == "Workspace notifications"


def _visibility(conn) -> str:
    return conn.execute(
        select(agent_sessions.c.visibility).where(agent_sessions.c.id == WORKSPACE_NOTICE_SESSION_ID)
    ).scalar_one()


# ── half one: hidden from ordinary Session lists ─────────────────────────────


def test_reserved_session_is_absent_from_both_ordinary_session_list_surfaces(
    monkeypatch, tmp_path: Path
) -> None:
    """RED before ``system``: the row was ``foreground`` and appeared in both lists.

    Two surfaces, because they are two queries with two filters: the workbench
    ``list_sessions`` (unscoped — the case a project filter would have masked) and the
    offset-paginated ``list_sessions_page`` behind ``vibe session list``. Both filter
    POSITIVELY on ``visibility == 'foreground'``, so ``system`` is excluded with no
    change of theirs; this test is what makes that a pinned property instead of an
    assumption about code neither this round nor the next one touches.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    ordinary, _ = _ordinary_session(db_path, channel="C700")

    with engine.connect() as conn:
        listed = _list_ids(conn)
        paged = _paged_ids(conn)
    assert WORKSPACE_NOTICE_SESSION_ID not in listed, (
        f"reserved system row leaked into the workbench session list: {listed}"
    )
    assert WORKSPACE_NOTICE_SESSION_ID not in paged, (
        f"reserved system row leaked into the paged CLI session list: {paged}"
    )
    # The filter is a projection, not a general blackout: ordinary sessions still list.
    assert ordinary in listed
    assert ordinary in paged


def test_reserved_session_is_absent_from_the_mention_title_search(monkeypatch, tmp_path: Path) -> None:
    """``#``-mention picker rides ``list_sessions``, so the same filter covers it."""
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    with engine.connect() as conn:
        hits = [row["id"] for row in wss.list_sessions(conn, limit=100, title_query="workspace")["sessions"]]
    assert hits == [], f"the reserved row is mentionable in the composer: {hits}"


def test_reserved_session_messages_stay_out_of_message_search(monkeypatch, tmp_path: Path) -> None:
    """DELIBERATE non-change: search keeps ``== 'foreground'``, narrower than the inbox.

    Search returns results a user opens and reads IN CONTEXT. The only system session
    holds machine-authored notices that are already surfaced as inbox cards, with no
    conversation around a hit to read — so admitting it would put runtime bookkeeping into
    every text search for no reachable next action. Pinned so the asymmetry with
    ``INBOX_SESSION_VISIBILITIES`` reads as a decision, not an oversight.

    Exercised with a ``result`` row on purpose: ``notify`` is not in
    ``types_with('searchable')``, so the type filter alone would make this test pass
    without saying anything about visibility. Both layers hold, and this asserts the one
    the round changed.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    with engine.begin() as conn:
        messages_service.append(
            conn,
            scope_id=None,
            session_id=WORKSPACE_NOTICE_SESSION_ID,
            platform="avibe",
            author="agent",
            message_type="result",
            text="unmistakable-needle-in-a-system-session",
        )
    with engine.connect() as conn:
        found = messages_service.search_messages(conn, platform="avibe", query="unmistakable-needle")
        # Control: the same predicate does find an ordinary session's result.
        ordinary, scope_id = _ordinary_session(db_path, channel="C703")
    with engine.begin() as conn:
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=ordinary,
            platform="avibe",
            author="agent",
            message_type="result",
            text="unmistakable-needle-in-an-ordinary-session",
        )
    with engine.connect() as conn:
        control = messages_service.search_messages(conn, platform="avibe", query="unmistakable-needle")
    assert found["sessions"] == [], f"system-session text is searchable: {found}"
    assert [s["session_id"] for s in control["sessions"]] == [ordinary]


# ── half two: still consumable through the ordinary inbox machinery ──────────


def test_reserved_session_is_admitted_to_the_inbox_projection(monkeypatch, tmp_path: Path) -> None:
    """The card, and therefore the SSE event and the push, survive being hidden.

    ``get_inbox_session`` returning a row is the actual gate for both realtime paths in
    ``core/message_mirror.py`` (``bus.publish('inbox.session.updated', inbox_row)`` and
    ``maybe_notify_inbox_message``), so this assertion is the delivery contract and not
    just a list membership.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    with engine.connect() as conn:
        feed = _inbox_ids(conn)
        card = messages_service.get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID)
    assert WORKSPACE_NOTICE_SESSION_ID in feed
    assert card is not None
    assert "delivery target missing" in (card["preview_text"] or "")
    # No project placement (scope_id is NULL, deliberately — a reserved scope would mint
    # a fake project in the sidebar), and the card renders anyway.
    assert card["scope_id"] is None


def test_unread_results_in_a_system_session_are_counted_like_any_other(
    monkeypatch, tmp_path: Path
) -> None:
    """The unread subselects use the NEGATIVE form, so they must name the admitted set.

    Today's notices are ``notify``, which is not an unread type at all — so this test
    writes a ``result`` to exercise the predicate directly. The point is agreement: the
    badge counts what the feed shows. Left as ``!= 'foreground'``, a system session would
    sit in the feed with its unread never counted, which is the same class of silent
    disagreement the whole ladder keeps producing.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    with engine.begin() as conn:
        messages_service.append(
            conn,
            scope_id=None,
            session_id=WORKSPACE_NOTICE_SESSION_ID,
            platform="avibe",
            author="agent",
            message_type="result",
            text="ack",
        )
    with engine.connect() as conn:
        per_session = messages_service.unread_counts_by_session(conn, platform="avibe")
        total = messages_service.total_unread(conn, platform="avibe")
    assert per_session.get(WORKSPACE_NOTICE_SESSION_ID) == 1
    assert total == 1


def test_a_failure_notify_alone_never_badges_the_system_session(monkeypatch, tmp_path: Path) -> None:
    """The projection did not turn failure notices into unread replies.

    ``notify`` is an inbox PREVIEW type but not an ``unread`` type, so admitting the
    session to the inbox raises a card without inflating the nav/PWA badge — which is the
    pre-existing rule for every failed turn, kept intact here.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    with engine.connect() as conn:
        assert WORKSPACE_NOTICE_SESSION_ID in _inbox_ids(conn)
        assert messages_service.unread_counts_by_session(conn, platform="avibe") == {}
        assert messages_service.total_unread(conn, platform="avibe") == 0


def test_background_would_hide_the_card_which_is_why_system_exists(monkeypatch, tmp_path: Path) -> None:
    """RED for the naive alternative: ``background`` satisfies only the hidden half.

    Kept as a permanent refutation. Anyone reducing the third value back to
    ``background`` gets both halves of the cost in one failure: the inbox card
    disappears (so the notice the rung exists to show is gone, while the notice row is
    still persisted and still stamped ``sent``), and the badge stops counting it.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_sessions SET visibility = 'background' WHERE id = :sid"),
            {"sid": WORKSPACE_NOTICE_SESSION_ID},
        )
    with engine.connect() as conn:
        assert WORKSPACE_NOTICE_SESSION_ID not in _list_ids(conn), "background does hide it"
        assert messages_service.get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is None, (
            "if this now returns a row, 'background' has become inbox-visible and the "
            "reason 'system' exists has been lost"
        )
        assert _inbox_ids(conn) == []
    # ``system`` restores the card without restoring the list entry: the next notice's
    # heal is what an install actually relies on.
    with engine.begin() as conn:
        resolve_workspace_notice_session(conn)
    with engine.connect() as conn:
        assert _visibility(conn) == "system"
        assert WORKSPACE_NOTICE_SESSION_ID not in _list_ids(conn)
        assert messages_service.get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is not None


def test_inbox_reads_see_a_system_row_written_on_another_connection(
    monkeypatch, tmp_path: Path
) -> None:
    """Two store instances over one file: the drain writes, a UI read must see the card.

    The notice drain and the request that renders the inbox are different processes in
    production, so they are different connections here — the projection must be a
    property of the DATABASE, not of one open transaction's snapshot. Nothing in it is
    connection-local (no temp view, no session pragma), and this is what says so.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    writer = create_sqlite_engine(db_path)
    reader = create_sqlite_engine(db_path)
    try:
        with writer.begin() as conn:
            resolve_workspace_notice_session(conn, title="Workspace notifications")
            messages_service.append(
                conn,
                scope_id=None,
                session_id=WORKSPACE_NOTICE_SESSION_ID,
                platform="avibe",
                author="agent",
                message_type=NOTICE_TYPE,
                text="watch 'ci' failed",
            )
        with reader.connect() as conn:
            assert messages_service.get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is not None
            assert WORKSPACE_NOTICE_SESSION_ID in _inbox_ids(conn)
            assert WORKSPACE_NOTICE_SESSION_ID not in _list_ids(conn)
            assert WORKSPACE_NOTICE_SESSION_ID not in _paged_ids(conn)
    finally:
        writer.dispose()
        reader.dispose()


# ── narrow protection: archive, update, scoped clear, delete ─────────────────


def test_update_session_refuses_the_reserved_id(monkeypatch, tmp_path: Path) -> None:
    """An unguarded PATCH could un-project the row until the next notice healed it.

    ``visibility='background'`` mutes the notice; ``'foreground'`` re-materializes a
    machine-owned chat in every session list; a ``scope_id`` mints a fake project AND
    puts the row inside a scoped clear's reach. All three are silent in between, so the
    refusal is on IDENTITY — before the existence read, so the answer does not depend on
    whether the lazily-created row exists yet.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = create_sqlite_engine(db_path)

    # Before the row exists at all.
    with engine.begin() as conn:
        with pytest.raises(PermissionError) as exc:
            wss.update_session(conn, WORKSPACE_NOTICE_SESSION_ID, visibility="foreground")
    assert getattr(exc.value, "code", None) == "reserved_session"

    with engine.begin() as conn:
        resolve_workspace_notice_session(conn, title="Workspace notifications")
    for kwargs in (
        {"visibility": "background"},
        {"visibility": "foreground"},
        {"title": "Chat with me"},
        {"scope_id": None},
        {"pinned": True},
    ):
        with engine.begin() as conn:
            with pytest.raises(PermissionError) as exc:
                wss.update_session(conn, WORKSPACE_NOTICE_SESSION_ID, **kwargs)
        assert getattr(exc.value, "code", None) == "reserved_session", kwargs
    # The refusal leaves the projection exactly as it was.
    with engine.connect() as conn:
        assert _visibility(conn) == "system"
        assert WORKSPACE_NOTICE_SESSION_ID not in _list_ids(conn)


def test_no_caller_can_promote_an_ordinary_session_to_system(monkeypatch, tmp_path: Path) -> None:
    """The other direction of the same guard: ``system`` is not user-assignable.

    Without this, ``PATCH /api/sessions/<id> {"visibility": "system"}`` would hide any
    chat from every session list while leaving it delivering into the inbox — a session
    the user can see notifications from and can no longer open from a list.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    ordinary, _ = _ordinary_session(db_path, channel="C701")
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        with pytest.raises(ValueError, match="invalid session visibility"):
            wss.update_session(conn, ordinary, visibility="system")
    # The two assignable values still work.
    with engine.begin() as conn:
        assert wss.update_session(conn, ordinary, visibility="background")["visibility"] == "background"
        assert wss.update_session(conn, ordinary, visibility="foreground")["visibility"] == "foreground"


def test_archive_still_refuses_the_reserved_id(monkeypatch, tmp_path: Path) -> None:
    """The projection narrows the blast radius; it does not replace the archive guard.

    The row is no longer one click from a session list, but the inbox card carries its
    id, so ``DELETE /api/sessions/ses-workspace-notices`` is still reachable by anyone
    holding it — and an archived row keeps earning receipts while showing no card.
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    with engine.begin() as conn:
        with pytest.raises(PermissionError) as exc:
            wss.archive_session(conn, WORKSPACE_NOTICE_SESSION_ID)
    assert getattr(exc.value, "code", None) == "reserved_session"
    with engine.connect() as conn:
        assert wss.is_session_archived(conn, WORKSPACE_NOTICE_SESSION_ID) is False
        assert messages_service.get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is not None


def test_the_row_accepts_no_turn_by_identity_or_by_projection(monkeypatch, tmp_path: Path) -> None:
    """``system`` keeps the row in the inbox, so its card is a clickable chat — and a
    chat has a composer. ``session_is_runtime_owned`` is the fact both the chat surface
    and ``POST /api/sessions/<id>/messages`` read to refuse that send (the plan's "no
    backend and no turns").

    Both halves of the OR are pinned because they cover different failures:

    * the PROJECTION half generalizes — a future ``system`` row is refused without a
      second line — and is what the loaded session payload can answer for free;
    * the IDENTITY half covers the window the heal has not reached yet. The row is
      repaired LAZILY (on the next notice), so between a drifted
      ``visibility='foreground'`` and that repair a projection-only test would admit a
      turn into the machine's row. Same reason the archive/update guards test identity.

    And an ordinary session must be untouched, or the guard breaks every send.
    """
    from storage.agent_session_rows import session_is_runtime_owned

    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)

    with engine.connect() as conn:
        reserved = wss.get_session(conn, WORKSPACE_NOTICE_SESSION_ID)
    assert reserved["visibility"] == WORKSPACE_NOTICE_SESSION_VISIBILITY
    assert session_is_runtime_owned(
        session_id=reserved["id"], visibility=reserved["visibility"]
    ) is True
    # Identity alone, i.e. a caller holding only the id (no row loaded) and a row whose
    # visibility drifted and has not been healed back yet.
    assert session_is_runtime_owned(session_id=WORKSPACE_NOTICE_SESSION_ID) is True
    assert session_is_runtime_owned(
        session_id=WORKSPACE_NOTICE_SESSION_ID, visibility="foreground"
    ) is True
    # Projection alone: a hypothetical second system-owned row inherits the refusal.
    assert session_is_runtime_owned(
        session_id="ses_some_other_system_row",
        visibility=WORKSPACE_NOTICE_SESSION_VISIBILITY,
    ) is True

    ordinary, _ = _ordinary_session(db_path, channel="C703")
    with engine.connect() as conn:
        row = wss.get_session(conn, ordinary)
    assert row["visibility"] == "foreground"
    assert session_is_runtime_owned(session_id=row["id"], visibility=row["visibility"]) is False
    for visibility in ("foreground", "background", None, ""):
        assert session_is_runtime_owned(session_id=ordinary, visibility=visibility) is False


def test_the_shared_target_resolver_refuses_the_reserved_row(monkeypatch, tmp_path: Path) -> None:
    """The composer is one door; ``resolve_session_id_target`` is all the others.

    Round-16 review thread 3678900318. The previous round refused the send inside
    ``POST /api/sessions/<id>/messages``, which is the door a HUMAN finds. Every other
    turn entry point reaches the runtime through the shared resolver instead —
    ``vibe agent run --session-id ses-workspace-notices`` (``cmd_agent_run`` resolves
    the pin on its ``session_policy in {"existing", "fork"}`` branch),
    ``vibe task/watch add --session-id``, and ``enqueue_session_callback`` — and that
    resolver refused only ARCHIVED rows. The reserved row is deliberately kept ACTIVE
    (``archive_session`` refuses its id), so it sailed straight through and one CLI line
    could enqueue a real turn into the machine's row, whose ``agent_backend`` is empty.

    ``reason="reserved"``, a NEW value rather than a reuse of ``archived`` or
    ``missing``: the row exists and is healthy, so nothing about the destination is
    dead. ``missing`` in particular is the one reason ``_execute_claimed_request``
    classifies as ``delivery_target_missing``, which here would send a reader hunting
    for a session sitting in their own inbox.

    Both halves of ``session_is_runtime_owned`` again, for the same two directions the
    composer guard needed them in — the IDENTITY half covers the window before the lazy
    heal, the ``system`` PROJECTION generalizes to a future runtime-owned row — and an
    ordinary session must still resolve, or the guard breaks every pinned definition.
    """
    from core.scheduled_tasks import UnresolvableSessionTarget, resolve_session_id_target

    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)

    with pytest.raises(UnresolvableSessionTarget) as exc:
        resolve_session_id_target(WORKSPACE_NOTICE_SESSION_ID, db_path=db_path)
    assert exc.value.session_id == WORKSPACE_NOTICE_SESSION_ID
    assert exc.value.reason == "reserved", (
        "a distinct reason, so no consumer of the other two mistakes this for one of "
        f"them: {exc.value.reason}"
    )
    assert exc.value.reason != "missing", (
        "``missing`` is the ``delivery_target_missing`` classifier — this row is right "
        "there in the inbox, so that label would be a lie"
    )
    assert WORKSPACE_NOTICE_SESSION_ID in str(exc.value), (
        f"the message a failed run settles with has to name the session: {exc.value}"
    )
    # Still a ValueError, so every pre-existing ``except ValueError`` caller (CLI, API,
    # watches) keeps refusing rather than crashing.
    assert isinstance(exc.value, ValueError)

    # The IDENTITY half: a drifted ``visibility`` the lazy heal has not reached yet.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_sessions SET visibility = 'foreground' WHERE id = :sid"),
            {"sid": WORKSPACE_NOTICE_SESSION_ID},
        )
    with pytest.raises(UnresolvableSessionTarget) as drifted:
        resolve_session_id_target(WORKSPACE_NOTICE_SESSION_ID, db_path=db_path)
    assert drifted.value.reason == "reserved"

    # ORDINARY SESSIONS ARE UNCHANGED, in BOTH of the two caller-assignable
    # visibilities — the control cyhhao asked for by name (comment 5124692513). The
    # guard's whole risk is over-reach: ``session_is_runtime_owned`` ORs an identity
    # test with a projection test, and a projection widened by one value would refuse
    # every ``background`` session, i.e. every ``create_once`` / ``create_per_run``
    # definition and every ``vibe agent run --create-session`` on the install. So both
    # are resolved here and their whole target is compared, not just the fact that no
    # exception was raised: ``suppress_delivery`` is DERIVED from ``visibility``
    # (``background`` suppresses), which is exactly the field a mis-widened predicate
    # would take out with it.
    ordinary, _scope_id = _ordinary_session(db_path, channel="C704")
    resolved = resolve_session_id_target(ordinary, db_path=db_path)
    assert resolved.session_id == ordinary
    assert resolved.session_key.to_key() == "slack::channel::C704"
    assert resolved.visibility == "foreground"
    assert resolved.suppress_delivery is False

    with engine.begin() as conn:
        assert wss.update_session(conn, ordinary, visibility="background")["visibility"] == "background"
    backgrounded = resolve_session_id_target(ordinary, db_path=db_path)
    assert backgrounded.session_id == ordinary, (
        "a BACKGROUND session is the ordinary shape of every reserved harness session — "
        "refusing it here would break every ``create_once`` definition on the install"
    )
    assert backgrounded.session_key.to_key() == "slack::channel::C704"
    assert backgrounded.visibility == "background"
    assert backgrounded.suppress_delivery is True, (
        "and its delivery suppression still derives from the same visibility the guard "
        "reads, so the two readings of that column cannot drift apart"
    )
    with engine.begin() as conn:
        wss.update_session(conn, ordinary, visibility="foreground")

    # The PROJECTION half: a hypothetical second runtime-owned row inherits the refusal
    # by visibility alone, with no second line here. Written by raw SQL because
    # ``system`` is not in ``ASSIGNABLE_SESSION_VISIBILITIES``.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_sessions SET visibility = 'system' WHERE id = :sid"),
            {"sid": ordinary},
        )
    with pytest.raises(UnresolvableSessionTarget) as projected:
        resolve_session_id_target(ordinary, db_path=db_path)
    assert projected.value.reason == "reserved"


def test_an_absent_row_still_reads_as_missing_not_reserved(monkeypatch, tmp_path: Path) -> None:
    """Existence is decided FIRST, so the reserved id does not shadow ``missing``.

    ``session_is_runtime_owned`` answers on the id alone, so a check placed before the
    existence test would relabel "the reserved row has been deleted and no notice has
    recreated it yet" as ``reserved`` — and ``missing`` is the reason the delivery
    classification and the binding recovery are both written against. Ordering, not a
    special case.
    """
    from core.scheduled_tasks import UnresolvableSessionTarget, resolve_session_id_target

    db_path = _migrated_db(monkeypatch, tmp_path)
    with pytest.raises(UnresolvableSessionTarget) as exc:
        resolve_session_id_target(WORKSPACE_NOTICE_SESSION_ID, db_path=db_path)
    assert exc.value.reason == "missing"


def test_a_scoped_clear_cannot_reach_the_reserved_row(monkeypatch, tmp_path: Path) -> None:
    """``scope_id IS NULL`` is what keeps ``/new`` and per-scope teardown off this row.

    ``delete_agent_sessions`` always narrows to one resolved ``scope_id``, and SQL
    equality never matches NULL — so the reserved row is unreachable by construction
    rather than by an exemption list. That is the same property that made a scope-less
    row the right shape in the first place (no fake project in the sidebar).
    """
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    _ordinary_session(db_path, channel="C702")

    service = SQLiteSessionsService(db_path)
    try:
        removed = service.delete_agent_sessions(scope_key="slack::channel::C702")
    finally:
        service.close()
    assert removed >= 1, "the clear has to actually clear the scope it names"
    with engine.connect() as conn:
        assert _visibility(conn) == "system"
        assert messages_service.get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is not None


def test_an_accepted_notice_prevents_raw_session_deletion(monkeypatch, tmp_path: Path) -> None:
    """Accepted communication keeps its Session graph intact."""
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM agent_sessions WHERE id = :sid"),
                {"sid": WORKSPACE_NOTICE_SESSION_ID},
            )
    with engine.connect() as conn:
        row = conn.execute(
            select(agent_sessions.c.visibility, agent_sessions.c.session_anchor, agent_sessions.c.status)
            .where(agent_sessions.c.id == WORKSPACE_NOTICE_SESSION_ID)
        ).mappings().one()
        assert messages_service.get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is not None
    assert row["visibility"] == "system"
    assert row["session_anchor"] == WORKSPACE_NOTICE_SESSION_ANCHOR
    assert row["status"] == "active"


def test_an_archived_reserved_row_heals_back_to_system(monkeypatch, tmp_path: Path) -> None:
    """The heal covers the states recreation cannot: the row exists and is unusable."""
    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_sessions SET status = 'archived', visibility = 'background', "
                "session_anchor = 'archived:' || id WHERE id = :sid"
            ),
            {"sid": WORKSPACE_NOTICE_SESSION_ID},
        )
    with engine.connect() as conn:
        assert messages_service.get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is None
    with engine.begin() as conn:
        resolve_workspace_notice_session(conn)
    with engine.connect() as conn:
        assert _visibility(conn) == "system"
        assert WORKSPACE_NOTICE_SESSION_ID not in _list_ids(conn)
        assert messages_service.get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is not None


# ── the Agents run graph ─────────────────────────────────────────────────────


def test_reserved_session_is_not_a_node_in_the_agents_graph(monkeypatch, tmp_path: Path) -> None:
    """The Agents graph is an ordinary session surface too, so ``system`` is not a node.

    ``build_graph``'s candidate set is the live sessions plus the sessions any ``agent_runs``
    row references. The reserved session runs nothing, so TODAY it is never a candidate —
    but that is an invariant of the notice path, not of the graph, and the graph's own
    visibility test is ``!= 'background'``, which ADMITS an unrecognized value. A single
    run pointing at the row (a repaired ``session_id``, imported state, a future self-heal
    run) put it in the History view as a node counted under ``foreground``. So the
    exclusion is explicit, and this test forces the candidate case rather than relying on
    the absence of runs: the synthetic run below is exactly what made the leak visible.
    """
    from core.services import agent_graph
    from storage.models import agent_runs

    db_path = _migrated_db(monkeypatch, tmp_path)
    engine = _reserved_with_one_notice(db_path)
    now = "2026-07-01T00:00:00+00:00"
    try:
        with engine.begin() as conn:
            conn.execute(
                agent_runs.insert().values(
                    id="run-into-the-system-session",
                    run_type="task",
                    status="succeeded",
                    session_id=WORKSPACE_NOTICE_SESSION_ID,
                    created_at=now,
                    updated_at=now,
                    completed_at=now,
                    cancel_requested=0,
                    metadata_json="{}",
                )
            )
        for include_ended in (True, False):
            for include_background in (True, False):
                graph = agent_graph.build_graph(
                    live_agents=[],
                    include_ended=include_ended,
                    include_background=include_background,
                    engine=engine,
                )
                ids = [node["session_id"] for node in graph["nodes"]]
                assert WORKSPACE_NOTICE_SESSION_ID not in ids, (
                    f"include_ended={include_ended} include_background={include_background}: "
                    f"reserved system row became a graph node: {ids}"
                )
                assert graph["counts"]["foreground"] == 0
    finally:
        engine.dispose()
