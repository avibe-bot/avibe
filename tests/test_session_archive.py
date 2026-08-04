"""Session archive is terminal: it reclaims bound resources and the archived
row becomes inert (never re-bound by inbound routing or task resolution)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import paths
from core.scheduled_tasks import resolve_session_id_target
from core.show_pages import ShowPageError, ShowPageStore
from core.show_session_events import ShowSessionEventError, ShowSessionEventStore
from storage import message_deliveries, messages_service
from storage import vault_service as vs
from storage import workbench_sessions_service as wss
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import (
    agent_runs,
    agent_sessions,
    message_deliveries as delivery_rows,
    run_definitions,
    show_pages,
    vault_grants,
    vault_requests,
)
from storage.vault_crypto import Sealed
from storage.session_reclaim import session_teardown_context
from storage.sessions_service import SQLiteSessionsService

NOW = "2026-06-08T00:00:00Z"


def _bind_session(service: SQLiteSessionsService, *, channel: str, anchor: str, native: str) -> str:
    sid = service.bind_agent_session(
        scope_key=f"slack::channel::{channel}",
        agent_name="claude",
        session_anchor=anchor,
        native_session_id=native,
    )
    assert sid is not None
    return sid


def _insert_def(conn, *, def_id: str, session_id: str, definition_type: str, deleted_at=None) -> None:
    conn.execute(
        run_definitions.insert().values(
            id=def_id,
            definition_type=definition_type,
            enabled=1,
            deleted_at=deleted_at,
            session_id=session_id,
            created_at=NOW,
            updated_at=NOW,
            metadata_json="{}",
        )
    )


def _insert_run(
    conn,
    *,
    run_id: str,
    session_id: str,
    status: str,
    delivery_id: str | None = None,
) -> None:
    conn.execute(
        agent_runs.insert().values(
            id=run_id,
            run_type="agent",
            status=status,
            session_id=session_id,
            delivery_id=delivery_id,
            cancel_requested=0,
            created_at=NOW,
            updated_at=NOW,
            metadata_json="{}",
        )
    )


def _grant_from_request(conn, request: dict, *, session_id: str, cache) -> dict:
    option = request["card"]["grant_options"][0]
    return vs.create_grant(
        conn,
        member_names=option["member_snapshot"],
        source_selector=option["source_selector"],
        purpose=option["purpose"],
        session_id=session_id,
        request_id=request["id"],
        cache=cache,
    )


def test_archive_reclaims_bound_resources(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C1", anchor="slack_C1", native="nat1")
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        wss.set_agent_status(conn, sid, "running")  # simulate a live "working" dot
        _insert_def(conn, def_id="task1", session_id=sid, definition_type="scheduled")
        _insert_def(conn, def_id="watch1", session_id=sid, definition_type="watch")
        # Already-deleted definition + a terminal run must NOT be counted/touched.
        _insert_def(conn, def_id="task_dead", session_id=sid, definition_type="scheduled", deleted_at=NOW)
        _insert_run(conn, run_id="run_q", session_id=sid, status="queued")
        _insert_run(conn, run_id="run_r", session_id=sid, status="running")
        _insert_run(conn, run_id="run_done", session_id=sid, status="succeeded")
        conn.execute(
            show_pages.insert().values(
                session_id=sid, visibility="public", share_id="shareX", created_at=NOW, updated_at=NOW
            )
        )
        vs.create_secret(conn, name="ARCHIVE_KEY", protection="protected", sealed=Sealed("ct", "nonce", "wrap"))
        vs.create_secret(conn, name="ARCHIVE_OTHER_KEY", protection="protected", sealed=Sealed("ct-other", "nonce-other", "wrap-other"))
        vs.create_secret(
            conn,
            name="ARCHIVE_RESERVED_KEY",
            protection="protected",
            sealed=Sealed("ct-reserved", "nonce-reserved", "wrap-reserved"),
            policy={"always_ask": True},
        )
        vs.create_secret(
            conn,
            name="ARCHIVE_SIGNING_KEY",
            protection="protected",
            kind="keypair",
            signer_kind="local",
            sealed=Sealed("ct-key", "nonce-key", "wrap-key"),
            public_meta={
                "signing_public_key": {
                    "curve": "secp256k1",
                    "public_key": "02" + "cd" * 32,
                }
            },
        )
        req = vs.create_access_request(conn, "ARCHIVE_KEY", requester={"session_id": sid}, delivery={"session_id": sid})
        pending_req = vs.create_access_request(
            conn,
            "ARCHIVE_KEY",
            requester={"session_id": sid},
            delivery={"session_id": sid, "command": "python sync.py"},
        )
        pending_uncovered_req = vs.create_access_request(
            conn,
            "ARCHIVE_OTHER_KEY",
            requester={"session_id": sid},
            delivery={"session_id": sid, "command": "python other.py"},
        )
        sign_req = vs.create_sign_request(
            conn,
            "ARCHIVE_SIGNING_KEY",
            digest="00" * 32,
            scheme="ecdsa-secp256k1-recoverable",
            requester={"session_id": sid},
            delivery={"session_id": sid},
        )
        other_req = vs.create_access_request(
            conn,
            "ARCHIVE_KEY",
            requester={"session_id": "ses_other"},
            delivery={"session_id": "ses_other"},
        )
        cache = vs.GRANT_RUNTIME_CACHE
        grant = _grant_from_request(conn, req, session_id=sid, cache=cache)
        reserved_req = vs.create_access_request(
            conn,
            "ARCHIVE_RESERVED_KEY",
            requester={"session_id": sid},
            delivery={"session_id": sid, "command": "python reserved.py"},
        )
        reserved_grant = _grant_from_request(conn, reserved_req, session_id=sid, cache=cache)
        reserved = vs.resolve_secret_access(
            conn,
            "ARCHIVE_RESERVED_KEY",
            requester={"session_id": sid},
            delivery={"session_id": sid, "command": "python reserved.py"},
            reserve_one_shot=True,
        )
        assert cache.has(grant["id"], "ARCHIVE_KEY")
        assert reserved["grant"]["status"] == "reserved"
        assert cache.has(reserved_grant["id"], "ARCHIVE_RESERVED_KEY")

    with engine.begin() as conn:
        result = wss.archive_session(conn, sid)

    assert result["status"] == "archived"
    assert result["agent_status"] == "idle"
    assert result["reclaimed"] == {"tasks": 1, "watches": 1, "runs": 2, "queued": 0}
    assert {scope["grant_id"] for scope in result["revoked_vault_grant_scopes"]} == {grant["id"], reserved_grant["id"]}

    with engine.connect() as conn:
        live_defs = (
            conn.execute(
                select(run_definitions.c.id)
                .where(run_definitions.c.session_id == sid)
                .where(run_definitions.c.deleted_at.is_(None))
            )
            .scalars()
            .all()
        )
        assert live_defs == []  # both live definitions soft-deleted

        runs = {
            r["id"]: r
            for r in conn.execute(select(agent_runs).where(agent_runs.c.session_id == sid)).mappings().all()
        }
        assert runs["run_q"]["status"] == "canceled"  # unstarted → terminalized
        assert runs["run_r"]["cancel_requested"] == 1  # in-flight → flagged for the executor
        assert runs["run_done"]["status"] == "succeeded"  # terminal → untouched

        page = conn.execute(select(show_pages).where(show_pages.c.session_id == sid)).mappings().first()
        assert page["visibility"] == "offline"
        assert page["offline_at"] is not None
        grant_row = conn.execute(select(vault_grants).where(vault_grants.c.id == grant["id"])).mappings().one()
        reserved_grant_row = conn.execute(
            select(vault_grants).where(vault_grants.c.id == reserved_grant["id"])
        ).mappings().one()
        assert grant_row["status"] == "revoked"
        assert reserved_grant_row["status"] == "revoked"
        assert not cache.has(grant["id"], "ARCHIVE_KEY")
        assert not cache.has(reserved_grant["id"], "ARCHIVE_RESERVED_KEY")
        request_statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                select(vault_requests.c.id, vault_requests.c.status).where(
                    vault_requests.c.id.in_(
                        [req["id"], pending_req["id"], pending_uncovered_req["id"], sign_req["id"], other_req["id"]]
                    )
                )
            ).mappings()
        }
        assert request_statuses[req["id"]] == "approved"
        assert request_statuses[pending_req["id"]] == "approved"
        assert request_statuses[pending_uncovered_req["id"]] == "expired"
        assert request_statuses[sign_req["id"]] == "expired"
        assert request_statuses[other_req["id"]] == "pending"


def test_hfr_287_new_session_teardown_publishes_run_updates_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from storage import background

    service = SQLiteSessionsService(tmp_path / "vibe.sqlite")
    published: list[tuple[set[str], dict[str, tuple[str, bool]]]] = []
    try:
        session_id = _bind_session(
            service,
            channel="C_NEW_WAKE",
            anchor="slack_new_wake",
            native="native-new-wake",
        )
        with service.engine.begin() as conn:
            _insert_run(
                conn,
                run_id="run-running",
                session_id=session_id,
                status="running",
            )
            _insert_run(
                conn,
                run_id="run-queued",
                session_id=session_id,
                status="queued",
            )

        def _capture(rows) -> None:  # noqa: ANN001
            with service.engine.connect() as conn:
                committed = {
                    str(row.id): (str(row.status), bool(row.cancel_requested))
                    for row in conn.execute(
                        select(
                            agent_runs.c.id,
                            agent_runs.c.status,
                            agent_runs.c.cancel_requested,
                        ).where(
                            agent_runs.c.id.in_(("run-running", "run-queued"))
                        )
                    )
                }
            published.append(({str(row["id"]) for row in rows}, committed))

        monkeypatch.setattr(background, "_publish_run_rows_updated", _capture)

        assert service.delete_agent_sessions(
            scope_key="slack::channel::C_NEW_WAKE",
            agent_name="claude",
        ) == 1
    finally:
        service.close()

    assert published == [
        (
            {"run-running", "run-queued"},
            {
                "run-running": ("running", True),
                "run-queued": ("canceled", True),
            },
        )
    ]


def test_archive_release_vault_scopes_runs_in_threadpool(monkeypatch) -> None:
    from vibe import api, ui_server

    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    release = Mock(side_effect=api.AvaultError("agent release failed"))
    monkeypatch.setattr(ui_server.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(api, "release_vault_agent_scopes", release)

    asyncio.run(
        ui_server._archive_release_vault_scopes(
            "ses_archive",
            [{"grant_id": "vgr_archive"}],
        )
    )

    assert calls
    release.assert_called_once_with(
        [{"grant_id": "vgr_archive"}],
        reason="archive_session:ses_archive",
    )


def test_archived_session_not_reused_for_anchor(monkeypatch, tmp_path: Path) -> None:
    """A new inbound message on the same thread must NOT resurrect an archived
    session, NOR collide on the (scope_id, session_anchor) UNIQUE index — a fresh
    row is bound instead.

    Runs on the MIGRATED schema (``ensure_sqlite_state`` → alembic), because that
    unique index only exists post-migration; ``metadata.create_all`` omits it, so
    a create_all-only test would miss the archived-row collision entirely."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C2", anchor="slack_C2", native="nat1")
        assert service.find_session_for_anchor(scope_key="slack::channel::C2", session_anchor="slack_C2") is not None

        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            wss.archive_session(conn, sid)

        # Anchor lookup skips the archived row (its anchor was vacated on archive).
        assert service.find_session_for_anchor(scope_key="slack::channel::C2", session_anchor="slack_C2") is None

        # Re-binding the same thread must NOT raise (unique index) and creates a
        # NEW row — never re-activating the archived one.
        sid2 = _bind_session(service, channel="C2", anchor="slack_C2", native="nat2")
        assert sid2 != sid
        assert service.get_agent_session_by_id(sid)["status"] == "archived"
        assert service.get_agent_session_by_id(sid2)["status"] == "active"
    finally:
        service.close()


def test_resolve_session_id_target_rejects_archived(tmp_path: Path) -> None:
    """A task/watch/run that targets an archived session by id is unresolvable
    (treated as invalid → the run is skipped)."""
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C3", anchor="slack_C3", native="nat1")
    finally:
        service.close()

    # Resolvable while active.
    resolve_session_id_target(sid, db_path=db_path)

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        wss.archive_session(conn, sid)

    with pytest.raises(ValueError, match="archived"):
        resolve_session_id_target(sid, db_path=db_path)


def _pending_ids(conn, session_id: str) -> list[str]:
    return list(
        conn.execute(
            select(delivery_rows.c.id)
            .where(delivery_rows.c.session_id == session_id)
            .where(delivery_rows.c.state == "reserved")
        ).scalars()
    )


def test_archive_clears_queued_and_pending_messages(tmp_path: Path) -> None:
    """Archive drops both send-while-busy queued prompts AND in-flight ``pending``
    send reservations, so neither can flush / be promoted into a terminal session."""
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C4", anchor="slack_C4", native="nat1")
        scope_id = service.get_agent_session_by_id(sid)["scope_id"]
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        queued_run_delivery = message_deliveries.enqueue_queued(
            conn,
            scope_id=scope_id,
            session_id=sid,
            text="q1",
        )
        message_deliveries.enqueue_queued(conn, scope_id=scope_id, session_id=sid, text="q2")
        _insert_run(
            conn,
            run_id="run_delivery_archive",
            session_id=sid,
            status="running",
            delivery_id=str(queued_run_delivery["id"]),
        )
        # A send mid-dispatch reserved its row as ``pending``; the user also has a
        # saved composer draft.
        message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_reserved_archive",
            session_id=sid,
            priority="p1",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope_id,
                session_id=sid,
                platform="avibe",
                author="user",
                source="user",
                text="reserved",
            ),
            dispatch_text="reserved",
        )
        message_deliveries.set_draft(conn, sid, "half-typed")
    with engine.connect() as conn:
        assert len(message_deliveries.list_queued(conn, sid)) == 2
        assert len(_pending_ids(conn, sid)) == 1
        assert message_deliveries.get_draft(conn, sid)
        # Queued prompts are surfaced in the reclaim preview (not silently dropped).
        assert wss.count_bound_resources(conn, sid)["queued"] == 2

    with engine.begin() as conn:
        wss.archive_session(conn, sid)
    with engine.connect() as conn:
        assert message_deliveries.list_queued(conn, sid) == []
        assert _pending_ids(conn, sid) == []
        assert not message_deliveries.get_draft(conn, sid)
        archived_run = conn.execute(
            select(agent_runs).where(agent_runs.c.id == "run_delivery_archive")
        ).mappings().one()
        assert archived_run["status"] == "canceled"
        assert archived_run["cancel_requested"] == 1
        assert archived_run["completed_at"] is not None


def test_archived_show_page_cannot_be_republished(monkeypatch, tmp_path: Path) -> None:
    """Archive takes the Show Page offline and locks it there — no re-sharing."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C5", anchor="slack_C5", native="nat1")
    finally:
        service.close()

    store = ShowPageStore()
    try:
        store.ensure(sid)
        store.update_visibility(sid, "public")  # allowed while active

        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            wss.archive_session(conn, sid)

        assert store.get(sid).visibility == "offline"  # archive took it offline
        with pytest.raises(ShowPageError):
            store.update_visibility(sid, "public")  # republish refused
    finally:
        store.close()


def test_republish_archived_session_creates_no_show_page(monkeypatch, tmp_path: Path) -> None:
    """Republishing an archived session that never had a page must NOT first
    materialize a default page (the guard runs before ``ensure``)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C9", anchor="slack_C9", native="nat1")
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        wss.archive_session(conn, sid)

    store = ShowPageStore()
    try:
        with pytest.raises(ShowPageError):
            store.update_visibility(sid, "public")
    finally:
        store.close()

    with engine.connect() as conn:
        row = conn.execute(select(show_pages.c.session_id).where(show_pages.c.session_id == sid)).first()
        assert row is None  # no page row was materialized


def test_bind_by_id_does_not_resurrect_archived(tmp_path: Path) -> None:
    """A late native-id bind (turn finishing after archive) must not flip an
    archived row back to active — archive is terminal."""
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C10", anchor="slack_C10", native="nat1")
        engine = create_sqlite_engine(db_path)
        with engine.begin() as conn:
            wss.archive_session(conn, sid)

        # The by-id bind targets the explicit archived row, bypassing the anchor
        # lookup guards — it must refuse rather than resurrect.
        assert service.bind_agent_session_by_id(session_id=sid, native_session_id="late-native") is None
        assert service.get_agent_session_by_id(sid)["status"] == "archived"
    finally:
        service.close()


def test_archived_session_excluded_from_inbox(monkeypatch, tmp_path: Path) -> None:
    """An archived session (and its unread) drops out of the inbox feed + badges."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C6", anchor="slack_C6", native="nat1")
        scope_id = service.get_agent_session_by_id(sid)["scope_id"]
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        # An unread agent ``result`` makes the session inbox-eligible.
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=sid,
            platform="avibe",
            author="agent",
            message_type="result",
            text="done",
        )
    with engine.connect() as conn:
        feed = messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"]
        assert any(s["session_id"] == sid for s in feed)
        assert sid in messages_service.unread_counts_by_session(conn, platform="avibe")

    with engine.begin() as conn:
        wss.archive_session(conn, sid)
    with engine.connect() as conn:
        feed = messages_service.list_inbox_sessions(conn, platform="avibe")["sessions"]
        assert all(s["session_id"] != sid for s in feed)
        assert sid not in messages_service.unread_counts_by_session(conn, platform="avibe")


def test_the_reserved_workspace_notice_session_cannot_be_archived(tmp_path: Path) -> None:
    """Archive is terminal, so it must not be reachable for the notice fallback row.

    D5 rung (5) resolves to a reserved workspace-notifications session
    (``storage.agent_session_rows.resolve_workspace_notice_session``). That row is
    ``foreground`` on purpose — ``background`` is filtered out of
    ``list_inbox_sessions``, which would hide the very notice the rung exists to show
    — so it appears in ordinary session lists and is ONE CLICK from this function.

    Archiving it is the worst reachable outcome in the whole ladder, and it is silent:
    ``persist_agent_message``'s ``_session_row`` has no status filter, so a later
    notice still WRITES its row and still earns its persisted receipt, so the rung
    ACKS and the notice is marked ``sent`` — while ``list_inbox_sessions`` excludes
    archived sessions, so there is no card, no ``inbox.session.updated`` and no push.
    Every subsequent caller-less failure would be recorded as delivered into a surface
    nothing displays, permanently. That is D1 broken again by D1's own fix.

    Two independent defences, and this is the first: refuse at the door. The second is
    ``resolve_workspace_notice_session``'s heal, pinned by
    ``test_an_archived_workspace_notice_session_heals_instead_of_swallowing_the_notice``,
    which covers a row archived out of band or before this guard existed.
    """
    from storage.agent_session_rows import (
        WORKSPACE_NOTICE_SESSION_ID,
        resolve_workspace_notice_session,
    )

    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    SQLiteSessionsService(db_path).close()  # create_all
    with engine.begin() as conn:
        reserved = resolve_workspace_notice_session(conn, title="Workspace notifications")
    assert reserved == WORKSPACE_NOTICE_SESSION_ID

    with engine.begin() as conn:
        with pytest.raises(PermissionError) as exc:
            wss.archive_session(conn, reserved)
    # A machine code, because the localized copy lives in ``vibe/i18n`` at the HTTP
    # boundary where the language is known — storage carries no user-facing text.
    assert getattr(exc.value, "code", None) == "reserved_session"

    with engine.connect() as conn:
        assert wss.is_session_archived(conn, reserved) is False, (
            "the refusal has to leave the row usable, not half-torn-down"
        )
    # And an ordinary session is unaffected: the guard is keyed on the reserved
    # identity, not on "looks like a notification session".
    service = SQLiteSessionsService(db_path)
    try:
        ordinary = _bind_session(service, channel="C9", anchor="slack_C9", native="nat1")
    finally:
        service.close()
    with engine.begin() as conn:
        wss.archive_session(conn, ordinary)
    with engine.connect() as conn:
        assert wss.is_session_archived(conn, ordinary) is True


def test_is_session_archived_flag(tmp_path: Path) -> None:
    """The shared write-guard accessor flips with the session's status."""
    db_path = tmp_path / "vibe.sqlite"
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C7", anchor="slack_C7", native="nat1")
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    with engine.connect() as conn:
        assert wss.is_session_archived(conn, sid) is False
        assert wss.is_session_archived(conn, "does-not-exist") is False
    with engine.begin() as conn:
        wss.archive_session(conn, sid)
    with engine.connect() as conn:
        assert wss.is_session_archived(conn, sid) is True


def test_show_event_rejected_for_archived_session(monkeypatch, tmp_path: Path) -> None:
    """An already-open Show Page can't write events into an archived session."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C8", anchor="slack_C8", native="nat1")
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        wss.archive_session(conn, sid)

    store = ShowSessionEventStore()
    try:
        with pytest.raises(ShowSessionEventError) as exc:
            store.append(
                sid,
                {
                    "type": "assistant.mark.created",
                    "mark": {"target": "mark-default-summary", "body": "x"},
                    "anchor": {"selector": "[mark-default='summary']", "text": "y"},
                },
            )
        assert exc.value.code == "session_archived"
    finally:
        store.close()


# --- P5 (PR5): hard-deleting a session must not orphan definitions bound to it ---
# Scenario IDs: HFR-049 (pause, never soft-delete) / HFR-050 (settings snapshot).


def test_delete_agent_sessions_reclaims_bound_definitions(monkeypatch, tmp_path: Path) -> None:
    """HFR-049 — the `/new` teardown path pauses bound definitions.

    ``/new`` reaches ``delete_agent_sessions`` (scope-wide via each backend's
    ``clear_agent_sessions``, and anchor-prefixed via ``clear_session_base``) and
    HARD-deletes the session rows. Nothing updated ``run_definitions.session_id``,
    so a ``create_once`` task pinned to that session fired and failed forever.

    Per D2 this path PAUSES (``enabled=0`` + ``last_error``), it does not
    soft-delete: archive is terminal, ``/new`` is an everyday command. Asserting
    only "it stopped firing" would pass against a soft-delete, so both halves are
    pinned here.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C9", anchor="slack_C9", native="nat1")
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == sid)
            .values(model="claude-sonnet-legacy", reasoning_effort="high")
        )
        _insert_def(conn, def_id="task1", session_id=sid, definition_type="scheduled")
        _insert_def(conn, def_id="watch1", session_id=sid, definition_type="watch")
        _insert_def(conn, def_id="task_dead", session_id=sid, definition_type="scheduled", deleted_at=NOW)

    service = SQLiteSessionsService(db_path)
    try:
        with session_teardown_context(reason="bound session cleared by /new") as reclaimed:
            removed = service.delete_agent_sessions(
                scope_key="slack::channel::C9",
                session_anchor_prefix="slack_C9",
            )
    finally:
        service.close()
    assert removed == 1
    # The teardown ledger is how `/new` reports "N tasks paused" without every
    # layer between the command handler and storage growing a return value.
    assert sorted(entry["definition_type"] for entry in reclaimed) == ["scheduled", "watch"]
    assert {entry["mode"] for entry in reclaimed} == {"pause"}

    with engine.connect() as conn:
        rows = {row["id"]: dict(row) for row in conn.execute(select(run_definitions)).mappings()}

    for def_id in ("task1", "watch1"):
        row = rows[def_id]
        assert row["deleted_at"] is None, f"{def_id} was soft-deleted; D2 requires a pause"
        assert row["enabled"] == 0, f"{def_id} still fires into a deleted session"
        assert row["last_error"], f"{def_id} paused with no explanation"
        assert "/new" in row["last_error"]
        # D3: the rebind must carry the old settings forward, and ``run_definitions``
        # has no model/reasoning_effort column — so reclaim is the only place that
        # still sees both rows.
        snapshot = json.loads(row["metadata_json"])["session_settings_snapshot"]
        assert snapshot["session_id"] == sid
        assert snapshot["model"] == "claude-sonnet-legacy"
        assert snapshot["reasoning_effort"] == "high"
        assert snapshot["agent_backend"] == "claude"

    # An already-reclaimed definition is untouched.
    assert rows["task_dead"]["deleted_at"] == NOW
    assert rows["task_dead"]["enabled"] == 1


def test_archive_still_soft_deletes_bound_definitions(monkeypatch, tmp_path: Path) -> None:
    """HFR-050 — the shared reclaim helper keeps archive terminal.

    ``reclaim_bound_definitions`` serves two callers with opposite outcomes. This
    is the guard that extracting it did not turn archive into a pause.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C10", anchor="slack_C10", native="nat1")
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update().where(agent_sessions.c.id == sid).values(model="opus-legacy")
        )
        _insert_def(conn, def_id="task_arch", session_id=sid, definition_type="scheduled")
        wss.archive_session(conn, sid)

    with engine.connect() as conn:
        row = dict(
            conn.execute(select(run_definitions).where(run_definitions.c.id == "task_arch")).mappings().one()
        )
    assert row["deleted_at"] is not None
    assert json.loads(row["metadata_json"])["session_settings_snapshot"]["model"] == "opus-legacy"


def test_a_teardown_that_rolls_back_reports_nothing_as_reclaimed(monkeypatch, tmp_path: Path) -> None:
    """HFR-273 — the ledger is live state, and it must roll back with its transaction.

    Found by applying HFR-271's rule (a rollback is proven only when the durable row and
    the live in-process state AGREE) to the one piece of teardown state that is not in
    the database. ``reclaim_bound_definitions`` appends to the teardown ledger as it
    pauses each definition, and the ledger is what ``handle_new`` counts to tell the user
    "N tasks paused". It was append-only: a transaction that reclaimed and then ABORTED
    -- a locked database on a later statement or on the COMMIT itself, the exact class of
    fault this whole PR is about -- rolled the pause back and left the entry standing.

    And nothing downstream corrects it. ``handle_new`` wraps ``clear_session_base`` in
    ``except Exception: logger.debug(...)`` and then reports the ledger anyway, so the
    user is told a task was paused while it is still enabled, still bound to the session
    they just cleared, and still firing into it every schedule.

    The fault is injected at the session DELETE, i.e. after the reclaim's UPDATE and
    inside the same transaction, so the database rolls both back together and the only
    question left is what the ledger says.
    """
    from sqlalchemy import event

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    service = SQLiteSessionsService(db_path)
    try:
        sid = _bind_session(service, channel="C7", anchor="slack_C7", native="nat1")
    finally:
        service.close()

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        _insert_def(conn, def_id="task1", session_id=sid, definition_type="scheduled")
        _insert_def(conn, def_id="watch1", session_id=sid, definition_type="watch")

    fired = {"count": 0}

    def _boom(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if not " ".join(statement.split()).upper().startswith("DELETE FROM AGENT_SESSIONS"):
            return
        fired["count"] += 1
        raise RuntimeError("teardown failed: database is locked")

    service = SQLiteSessionsService(db_path)
    event.listens_for(service.engine, "before_cursor_execute")(_boom)
    outcome: dict = {}
    try:
        with session_teardown_context(reason="bound session cleared by /new") as reclaimed:
            try:
                service.delete_agent_sessions(
                    scope_key="slack::channel::C7",
                    session_anchor_prefix="slack_C7",
                )
            except Exception as exc:  # noqa: BLE001 - the injected fault is the point
                outcome["error"] = exc
            # Read INSIDE the context: ``handle_new`` swallows the failure above and
            # reports the ledger from here, with the teardown context still open.
            reported = list(reclaimed)
    finally:
        service.close()

    assert fired["count"] >= 1, "the session DELETE never ran, so no fault was injected"
    assert outcome.get("error") is not None, "the injected fault did not reach the caller"

    with engine.connect() as conn:
        rows = {row["id"]: dict(row) for row in conn.execute(select(run_definitions)).mappings()}
    assert rows["task1"]["enabled"] == 1 and rows["watch1"]["enabled"] == 1, (
        "the transaction did not roll the reclaim back, so this test cannot tell a "
        "rolled-back ledger from an accurate one"
    )

    assert reported == [], (
        "the transaction rolled back and the LIVE ledger did not: /new reports "
        f"{len(reported)} definition(s) paused ({[entry.get('definition_id') for entry in reported]}) "
        "that are still enabled, still bound to the session the user just cleared, and "
        "still firing into it"
    )
