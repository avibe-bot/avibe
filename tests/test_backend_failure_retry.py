from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from core.session_turns import DeliveryRequest
from storage import message_deliveries, messages_service
from storage.agent_session_rows import reserve_write_lock
from storage.db import create_sqlite_engine
from storage.models import messages, session_turns
from tests.backend_failure_retry_helpers import reserve_failure_retry, seed_failed_notice
from tests.test_session_delivery_fsm import _context, _fsm_schema_template, managers  # noqa: F401
from tests.test_ui_session_stream import _accepted_dispatch, _make_session, isolated_state  # noqa: F401
from tests.ui_server_test_helpers import csrf_headers


def _retry_notice(tmp_path, *, backend="claude", not_written=False, content=None):
    scope_id, session_id = _make_session(tmp_path, agent_backend=backend)
    with create_sqlite_engine().begin() as conn:
        notice, turn, inputs = seed_failed_notice(
            conn,
            session_id=session_id,
            scope_id=scope_id,
            backend=backend,
            not_written=not_written,
            content=content,
        )
    return session_id, notice, turn, inputs


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_failure_retry_canonical_continue_and_draft(isolated_state, tmp_path, backend):
    from vibe.ui_server import app

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path, backend=backend)
    client = app.test_client()
    headers = csrf_headers(client)
    draft = client.put(
        f"/api/sessions/{session_id}/draft",
        headers=headers,
        json={"text": "不要丢掉这段草稿", "expected_updated_at": None},
    ).get_json()["draft"]
    dispatch = _accepted_dispatch(session_id)
    with patch("vibe.internal_client.dispatch_async", dispatch):
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            headers=headers,
            json={
                "retry_for": notice["id"],
                "text": "replay everything",
                "content": {"attachments": [{"token": "forged"}]},
                "metadata": {"quick_reply_for": "forged", "resource_user_context": {}},
            },
        )
    assert response.status_code == 201
    body = response.get_json()
    assert body["text"] == "continue"
    assert body["draft_advanced"] is False
    assert body["draft"] == draft
    assert body["retry_notice"]["content"]["failure_retry"]["state"] == "accepted"
    assert dispatch.await_args.args[0]["files"] == []
    with create_sqlite_engine().connect() as conn:
        reloaded = messages_service.list_session_messages(conn, session_id=session_id)
    persisted = next(row for row in reloaded["messages"] if row["id"] == notice["id"])
    assert persisted["content"]["failure_retry"]["state"] == "accepted"


@pytest.mark.parametrize("exception_name", ["InternalServerTimeout", "InternalServerUnavailable"])
def test_failure_retry_dispatch_loss(isolated_state, tmp_path, exception_name):
    from vibe import internal_client
    from vibe.ui_server import app

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path)
    client = app.test_client()
    headers = csrf_headers(client)
    url = f"/api/sessions/{session_id}/messages"
    with patch("vibe.internal_client.dispatch_async", side_effect=getattr(internal_client, exception_name)("offline")):
        failed = client.post(url, headers=headers, json={"retry_for": notice["id"]})
    assert failed.status_code in {502, 504}
    first_id = failed.get_json()["id"]
    with patch("vibe.internal_client.dispatch_async", _accepted_dispatch(session_id)):
        recovered = client.post(url, headers=headers, json={"retry_for": notice["id"]})
    assert recovered.status_code == 201
    assert (recovered.get_json()["id"] == first_id) is (exception_name == "InternalServerTimeout")
    with create_sqlite_engine().connect() as conn:
        visible = messages_service.list_session_messages(conn, session_id=session_id)["messages"]
    assert sum(row["text"] == "continue" for row in visible) == 1


@pytest.mark.parametrize("change", ["ordinary", "detached", "unlinked", "wrong_session", "stopped", "unknown", "newer"])
def test_failure_retry_invalid_or_stale_notice(isolated_state, tmp_path, change):
    from vibe.ui_server import app

    session_id, notice, turn, _inputs = _retry_notice(tmp_path)
    with create_sqlite_engine().begin() as conn:
        meta = dict(notice["metadata"])
        if change == "ordinary":
            meta["event"] = "progress"
        elif change == "detached":
            meta["detached"] = True
        elif change == "unlinked":
            meta.pop("turn_id")
        elif change == "wrong_session":
            meta["turn_id"] = "trn_missing"
        elif change == "stopped":
            conn.execute(
                update(session_turns).where(session_turns.c.id == turn["id"]).values(terminal_outcome="canceled")
            )
        elif change == "unknown":
            conn.execute(
                update(session_turns).where(session_turns.c.id == turn["id"]).values(start_receipt_outcome="unknown")
            )
        elif change == "newer":
            seed_failed_notice(conn, session_id=session_id, scope_id=notice["scope_id"], backend="claude")
        conn.execute(update(messages).where(messages.c.id == notice["id"]).values(metadata_json=json.dumps(meta)))
    client = app.test_client()
    with patch("vibe.internal_client.dispatch_async", AsyncMock()) as dispatch:
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == 409
    dispatch.assert_not_awaited()


def test_failure_retry_retains_prompt_and_attachment(isolated_state, tmp_path):
    from vibe.ui_server import app

    session_id, notice, _turn, inputs = _retry_notice(
        tmp_path,
        not_written=True,
        content={"attachments": [{"token": "server-owned-token", "name": "中文.png"}]},
    )

    async def dispatch(payload):
        assert payload["message_id"] == inputs[0]["id"]
        assert payload["text"] == "检查这张图"
        assert payload["files"] == [{"path": "/isolated/中文.png"}]
        return {"status_code": 202, "body": {"delivery_state": "queued", "queued": True}}

    client = app.test_client()
    with (
        patch("vibe.internal_client.dispatch_async", side_effect=dispatch),
        patch(
            "core.workbench_media.resolve_attachment_specs", return_value=[{"path": "/isolated/中文.png"}]
        ) as resolve,
    ):
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == 202
    assert resolve.call_args.kwargs["attachments"][0]["token"] == "server-owned-token"
    with create_sqlite_engine().connect() as conn:
        retained = message_deliveries.get_delivery(conn, inputs[0]["id"])
    assert retained["snapshot_json"] == inputs[0]["snapshot_json"]


def test_failure_retry_concurrent_clicks_have_one_owner(managers):
    """MESSAGE-DELIVERY-027: duplicate notice clicks share one native start."""
    manager_a, manager_b, engine_a, engine_b, starts = managers
    with engine_a.begin() as conn:
        notice, _turn, _inputs = seed_failed_notice(conn, session_id="ses_fsm")

    def reserve(engine):
        with engine.begin() as conn:
            reserve_write_lock(conn)
            return reserve_failure_retry(conn, notice)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, [engine_a, engine_b]))
    assert results[0]["id"] == results[1]["id"]

    async def admit_both():
        await asyncio.gather(
            *[
                manager.deliver(
                    DeliveryRequest(
                        session_id="ses_fsm",
                        priority="p3",
                        content="forged",
                        delivery_id=results[0]["id"],
                    ),
                    context=_context(),
                )
                for manager in [manager_a, manager_b]
            ]
        )

    asyncio.run(admit_both())
    assert len(starts) == 1
    assert starts[0][1] == "continue"


@pytest.mark.parametrize("new_work", ["reserved", "active", "completed"])
def test_failure_retry_admission_rejects_newer_work(managers, new_work):
    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        notice, _turn, _inputs = seed_failed_notice(conn, session_id="ses_fsm")
        retry = reserve_failure_retry(conn, notice)
        if new_work == "reserved":
            message_deliveries.insert_delivery(
                conn,
                delivery_id=message_deliveries.new_delivery_id(),
                session_id="ses_fsm",
                priority="p3",
                state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=None, session_id="ses_fsm", platform="avibe", author="user", source="user", text="new task"
                ),
                dispatch_text="new task",
            )
        else:
            _notice, newer, _inputs = seed_failed_notice(conn, session_id="ses_fsm")
            if new_work == "active":
                conn.execute(
                    update(session_turns)
                    .where(session_turns.c.id == newer["id"])
                    .values(
                        state="active",
                        terminal_outcome=None,
                        terminal_at=None,
                    )
                )
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="continue", delivery_id=retry["id"]),
            context=_context(),
        )
    )
    assert result.state == "retired"
    assert result.reason in {"retry_stale", "retry_pending_input"}
    assert starts == []


@pytest.mark.parametrize("texts", [["原输入"], ["原输入一", "原输入二"]])
def test_failure_retry_unwritten_original_batch(managers, texts):
    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        notice, _turn, originals = seed_failed_notice(
            conn,
            session_id="ses_fsm",
            not_written=True,
            texts=texts,
        )
        retry = reserve_failure_retry(conn, notice)
    assert retry["id"] == originals[0]["id"]
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="continue", delivery_id=retry["id"]),
            context=_context(),
        )
    )
    assert result.state == "claimed"
    assert len(starts) == 1
    assert all(text in starts[0][1] for text in texts)
    assert "continue" not in starts[0][1]


@pytest.mark.parametrize("not_written", [False, True])
def test_failure_retry_web_controller_round_trip(isolated_state, tmp_path, monkeypatch, not_written):
    """MESSAGE-DELIVERY-026: Web -> real admission -> native boundary -> replay."""
    import httpx
    from core import internal_server
    from tests.test_internal_server import _bind_test_native_start, _build_controller_double
    from vibe import ui_server

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path, not_written=not_written)
    engine = create_sqlite_engine()
    controller = _build_controller_double()
    internal_app = internal_server.create_app(controller)
    manager = controller.session_turns
    starts = []

    async def capture_run(sid, context, text, *, logical_turn_id=None, **kwargs):
        starts.append((sid, text))
        _bind_test_native_start(engine, context)
        manager._terminalize_durable_turn(
            logical_turn_id,
            "completed",
            settled_by="terminal_result",
            evidence_kind="test_terminal",
        )

    manager._run = capture_run
    transport = httpx.ASGITransport(app=internal_app)

    async def dispatch(payload):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as internal:
            response = await internal.post("/internal/dispatch_async", json=payload)
        return {"status_code": response.status_code, "body": response.json()}

    monkeypatch.setattr("vibe.internal_client.dispatch_async", dispatch)
    client = ui_server.app.test_client()
    headers = csrf_headers(client)
    url = f"/api/sessions/{session_id}/messages"
    first = client.post(url, json={"retry_for": notice["id"]}, headers=headers)
    duplicate = client.post(url, json={"retry_for": notice["id"]}, headers=headers)
    assert first.status_code == duplicate.status_code == 201
    assert first.get_json()["id"] == duplicate.get_json()["id"]
    assert starts == [(session_id, "检查这张图" if not_written else "continue")]


def test_failure_retry_queue_drain_rechecks_the_boundary(managers):
    """MESSAGE-DELIVERY-028: a deferred retry cannot overtake newer work."""
    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        notice, _turn, _inputs = seed_failed_notice(conn, session_id="ses_fsm")
        retry = reserve_failure_retry(conn, notice)

    async def queue_then_drain():
        manager.begin_backend_drain("codex")
        queued = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="continue", delivery_id=retry["id"]),
            context=_context(),
        )
        assert queued.state == "queued"
        with engine.begin() as conn:
            seed_failed_notice(conn, session_id="ses_fsm")
        await manager.end_backend_drain("codex")

    asyncio.run(queue_then_drain())
    with engine.connect() as conn:
        assert message_deliveries.get_delivery(conn, retry["id"])["state"] == "retired"
    assert starts == []


@pytest.mark.parametrize("restriction", ["archived", "access", "cross_session"])
def test_failure_retry_keeps_session_access_gates(isolated_state, tmp_path, restriction):
    from core.services import sessions
    from storage.models import agent_sessions
    from vibe.ui_server import app

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path)
    with create_sqlite_engine().begin() as conn:
        if restriction == "archived":
            conn.execute(update(agent_sessions).where(agent_sessions.c.id == session_id).values(status="archived"))
        elif restriction == "cross_session":
            original = sessions.get_session(conn, session_id)
            session_id = sessions.create_session(
                conn,
                scope_id=notice["scope_id"],
                agent_backend="claude",
                agent_id=original["agent_id"],
                agent_name=original["agent_name"],
            )["id"]
    client = app.test_client()
    with (
        patch("vibe.internal_client.dispatch_async", AsyncMock()) as dispatch,
        patch(
            "core.vibe_agents.ensure_session_agent_access",
            side_effect=PermissionError("denied") if restriction == "access" else None,
        ),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == (403 if restriction == "access" else 409)
    dispatch.assert_not_awaited()


def test_unwritten_retry_remains_clickable_when_controller_cannot_be_reached(isolated_state, tmp_path):
    from vibe import internal_client, ui_server

    session_id, notice, _turn, _inputs = _retry_notice(tmp_path, not_written=True)
    client = ui_server.app.test_client()
    with patch("vibe.internal_client.dispatch_async", side_effect=internal_client.InternalServerUnavailable("offline")):
        response = client.post(
            f"/api/sessions/{session_id}/messages", headers=csrf_headers(client),
            json={"retry_for": notice["id"]},
        )
    assert response.status_code == 502
    assert response.get_json()["retry_notice"]["content"]["failure_retry"]["state"] == "reserved"
    with create_sqlite_engine().connect() as conn:
        persisted = messages_service.get_message(conn, notice["id"])
    assert persisted["content"]["failure_retry"]["state"] == "reserved"


def test_stale_retry_releases_a_newer_input_waiting_behind_its_reservation(managers):
    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        notice, _turn, _inputs = seed_failed_notice(conn, session_id="ses_fsm")
        retry = reserve_failure_retry(conn, notice)

    async def admit():
        newer = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="new task"),
            context=_context(),
        )
        assert newer.state == "queued"  # the reserved retry is an ordering fence
        refused = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="continue", delivery_id=retry["id"]),
            context=_context(),
        )
        assert refused.state == "retired"
        assert refused.turn_id is None

    asyncio.run(admit())
    assert len(starts) == 1
    assert starts[0][1] == "new task"
