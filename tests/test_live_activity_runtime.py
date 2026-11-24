"""I5: real SQLite tracking and public HTTP lifecycle, with only relay IO replaced."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from storage import message_deliveries, web_push_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import session_turns, web_push_subscriptions
from tests.test_turn_last_reply_store import _seed, SESSION, TURN
from tests.ui_server_test_helpers import csrf_headers

REGISTRATION = "r" * 43
TOKEN = "ab" * 32
BODY = {
    "session_id": SESSION, "turn_id": TURN, "push_registration_id": REGISTRATION, "activity_token": TOKEN,
    "requested_at": int(datetime.now(timezone.utc).timestamp()),
}


@pytest.fixture
def state():
    from config.v2_config import V2Config

    V2Config.default().save()
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        starting = _seed(conn)
        message_deliveries.bind_native_start(
            conn,
            TURN,
            expected_version=starting["version"],
            runtime_key="runtime",
            runtime_turn_id=TURN,
            native_turn_id=TURN,
        )
        web_push_service.upsert_subscription(
            conn,
            user_key="local",
            payload={
                "endpoint": f"https://avibe.bot/api/push/apns/{REGISTRATION}",
                "keys": {"p256dh": "test", "auth": "test"},
            },
        )
    yield engine
    engine.dispose()


def service_for(engine):
    from core.live_activities import LiveActivityService

    now = [int(datetime.now(timezone.utc).timestamp())]
    sent = []
    service = LiveActivityService(engine=engine, clock=lambda: now[0], sender=lambda **kw: sent.append(kw["body"]))
    return service, now, sent


@pytest.mark.parametrize("stop_reason", ["instance_switch", "replaced"])
def test_public_http_register_rotate_end_and_owner_isolation(state, monkeypatch, stop_reason):
    from vibe import internal_client, ui_server

    service, now, sent = service_for(state)
    monkeypatch.setattr(internal_client, "live_activity_request", service.handle)
    client = ui_server.app.test_client()
    headers = csrf_headers(client)
    base = "/api/web-push/live-activities"
    response = client.get(f"{base}/turn?session_id={SESSION}")
    assert response.status_code == 200
    assert response.get_json()["turn"]["turn_id"] == TURN
    response = client.put(f"{base}/activity-1", json=BODY, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["activity"]["phase"] == "working"
    assert sent == []
    response = client.put(f"{base}/activity-1", json={**BODY, "activity_token": "cd" * 32}, headers=headers)
    assert response.status_code == 200
    monkeypatch.setattr(ui_server, "_web_push_user_key", lambda: "remote:other")
    assert client.get(f"{base}/activity-1").status_code == 404
    assert client.put(f"{base}/activity-2", json=BODY, headers=headers).status_code == 403
    monkeypatch.setattr(ui_server, "_web_push_user_key", lambda: "local")
    response = client.delete(f"{base}/activity-1", json={"reason": stop_reason}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["stopped"] is True
    assert client.get(f"{base}/activity-1").get_json()["activity"]["status"] == "stopped"
    from storage.live_activity_service import get
    with state.connect() as conn:
        assert get(conn, "activity-1")["stop_reason"] == stop_reason
    assert client.delete(
        f"{base}/activity-1", json={"reason": stop_reason}, headers=headers
    ).get_json()["stopped"] is False
    assert client.put(f"{base}/activity-1", json=BODY, headers=headers).get_json()["activity"]["status"] == "stopped"
    assert client.put(
        f"{base}/activity-1", json={**BODY, "activity_token": "ef" * 32}, headers=headers
    ).get_json()["activity"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_publisher_phase_heartbeat_terminal_and_restart(state):
    service, now, sent = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)
    await service.tick()
    assert sent == []
    now[0] += 900
    await service.tick()
    assert sent[-1]["priority"] == 5
    assert sent[-1]["stale_at"] == now[0] + 2400
    service.pending_question = lambda sid: sid == SESSION
    await service.tick()
    assert sent[-1]["phase"] == "needsYou"
    assert sent[-1]["alert"] == "needsYou"
    assert sent[-1]["timestamp"] > sent[-2]["timestamp"]
    restarted, _, _ = service_for(state)
    restarted.clock = service.clock
    restarted.sender = service.sender
    restarted.pending_question = service.pending_question
    await restarted.tick()
    assert len(sent) == 2
    with state.begin() as conn:
        message_deliveries.terminalize_turn(
            conn, TURN, outcome="completed", settled_by="terminal_result", evidence_kind="test"
        )
    await restarted.tick()
    assert sent[-1]["event"] == "end"
    assert sent[-1]["dismiss_at"] == sent[-1]["ended_at"] + 3600
    await restarted.tick()
    assert len(sent) == 3
    result = await restarted.handle("get", "local", "activity-1", {})
    assert result["body"]["activity"]["status"] == "ended"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,phase", [("completed", "done"), ("failed", "failed"), ("canceled", "stopped"), ("not_written", "failed")]
)
async def test_terminal_race_has_ended_state_and_no_start_push(state, outcome, phase):
    service, _, sent = service_for(state)
    with state.begin() as conn:
        # not_written is a pre-acceptance terminal; use its real state transition.
        if outcome == "not_written":
            conn.execute(
                session_turns.update()
                .where(session_turns.c.id == TURN)
                .values(
                    state="starting",
                    start_receipt_outcome=None,
                )
            )
        message_deliveries.terminalize_turn(
            conn, TURN, outcome=outcome, settled_by="terminal_result", evidence_kind="test"
        )
    result = await service.handle("put", "local", "activity-raced", BODY)
    assert result["status_code"] == 409
    assert result["body"]["activity"]["phase"] == phase
    assert result["body"]["activity"]["status"] == "ended"
    await service.tick()
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["vault_requester", "vault_delivery", "question", "computer_confirm"])
async def test_every_owner_request_source_uses_the_same_phase_projection(state, source):
    import json
    from types import SimpleNamespace
    from core.live_activities import controller_pending_question
    from core.computer_script_interactions import ComputerScriptInteractionManager, _PendingConfirm
    from modules.agents.question_ui import QuestionUIHandler, PendingQuestion
    from storage.models import vault_requests
    from tests.test_session_delivery_fsm import _context

    service, _, sent = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(agents={}, _turn_gates={}), computer_script_interactions=None
    )
    clear = lambda: None
    if source.startswith("vault_"):
        with state.begin() as conn:
            conn.execute(
                vault_requests.insert().values(
                    id="request-1",
                    request_type="access",
                    status="pending",
                    created_at="2026-11-20T00:00:00Z",
                    **{source.removeprefix("vault_"): json.dumps({"session_id": SESSION})},
                )
            )

        def clear():
            with state.begin() as conn:
                conn.execute(vault_requests.update().values(status="approved"))
    elif source == "question":
        # The backend's key differs from the durable Session id.
        handler = QuestionUIHandler(controller, None, None, "test")
        handler.set_pending(
            "backend-key",
            PendingQuestion(questions=[], prompt_text="Continue?", option_labels=[], base_session_id="backend-key"),
        )
        agent = SimpleNamespace(_question_handler=handler)
        controller.agent_service.agents = {"claude": agent}
        controller.agent_service._turn_gates = {
            "runtime": SimpleNamespace(
                token="active-token",
                agent=agent,
                backend="claude",
                request=SimpleNamespace(base_session_id="backend-key", context=_context(SESSION)),
            )
        }
        clear = lambda: handler.pop_pending("backend-key")
    else:
        manager = ComputerScriptInteractionManager(controller, None)
        question = PendingQuestion(questions=[], prompt_text="Continue?", option_labels=[], base_session_id=SESSION)
        record = _PendingConfirm(
            token="test",
            turn=None,
            context=_context(SESSION),
            question=question,
            future=asyncio.get_running_loop().create_future(),
            armed=True,
        )
        manager._pending["confirm"] = record
        controller.computer_script_interactions = manager
        clear = lambda: record.future.set_result(True)
    service.pending_question = lambda sid: controller_pending_question(controller, sid)
    await service.tick()
    assert sent[-1]["phase"] == "needsYou"
    assert sent[-1]["alert"] == "needsYou"
    clear()
    await service.tick()
    assert sent[-1]["phase"] == "working"
    assert "alert" not in sent[-1]


@pytest.mark.asyncio
async def test_expiration_replacement_conflict_and_device_token_survives_410(state):
    import httpx

    service, now, sent = service_for(state)
    assert (await service.handle("put", "local", "activity-1", BODY))["status_code"] == 200
    assert (await service.handle("put", "local", "activity-1", {**BODY, "turn_id": "other"}))["status_code"] == 409
    await service.handle("put", "local", "activity-2", BODY)
    assert (await service.handle("get", "local", "activity-1", {}))["body"]["activity"]["status"] == "stopped"

    def gone(**kwargs):
        response = httpx.Response(410, request=httpx.Request("POST", "https://avibe.bot"))
        raise httpx.HTTPStatusError("gone", request=response.request, response=response)

    service.sender = gone
    service.pending_question = lambda sid: True
    await service.tick()
    assert (await service.handle("get", "local", "activity-2", {}))["body"]["activity"]["status"] == "stopped"
    with state.connect() as conn:
        assert conn.execute(select(web_push_subscriptions.c.enabled)).scalar_one() == 1
    await service.handle("put", "local", "activity-3", BODY)
    now[0] += 28800
    service.sender = lambda **kwargs: sent.append(kwargs)
    await service.tick()
    assert sent == []
    assert (await service.handle("get", "local", "activity-3", {}))["body"]["activity"]["status"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503, 400])
async def test_phase_retry_is_bounded_and_honors_retry_after_across_restart(state, status):
    import httpx

    service, now, _ = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)
    sent = []

    def failed(**kwargs):
        sent.append(kwargs["body"])
        response = httpx.Response(
            status, headers={"Retry-After": "30"}, request=httpx.Request("POST", "https://avibe.bot")
        )
        raise httpx.HTTPStatusError("retry", request=response.request, response=response)

    service.sender = failed
    service.pending_question = lambda sid: True
    await service.tick()
    await service.tick()
    assert len(sent) == 1
    # A fresh service reads retry bounds and time from SQLite.
    restarted, _, _ = service_for(state)
    restarted.clock, restarted.sender, restarted.pending_question = service.clock, failed, service.pending_question
    for _ in range(6):
        now[0] += 30
        await restarted.tick()
    assert len(sent) == (4 if status in {429, 503} else 1)
    assert len({body["timestamp"] for body in sent}) == len(sent)


@pytest.mark.asyncio
async def test_failed_heartbeat_is_pending_and_retried_at_low_priority(state):
    import httpx

    service, now, _ = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)
    sent = []

    def failed(**kwargs):
        sent.append(kwargs["body"])
        response = httpx.Response(503, request=httpx.Request("POST", "https://avibe.bot"))
        raise httpx.HTTPStatusError("unavailable", request=response.request, response=response)

    service.sender = failed
    now[0] += 900
    await service.tick()
    now[0] += 30
    await service.tick()
    assert len(sent) == 2 and all(body["priority"] == 5 and "alert" not in body for body in sent)


@pytest.mark.asyncio
async def test_delete_joins_an_inflight_send_then_no_push_follows(state):
    import threading

    service, now, sent = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)
    entered, release = threading.Event(), threading.Event()

    def send(**kwargs):
        entered.set()
        assert release.wait(3)
        sent.append(kwargs["body"])

    service.sender = send
    service.pending_question = lambda sid: True
    publishing = asyncio.create_task(service.tick())
    assert await asyncio.to_thread(entered.wait, 3)
    stopping = asyncio.create_task(service.handle("delete", "local", "activity-1", {"reason": "dismissed"}))
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await publishing
    assert (await stopping)["body"]["stopped"] is True
    now[0] += 1800
    await service.tick()
    assert len(sent) == 1


def test_public_http_validates_inputs_and_session_visibility(state, monkeypatch):
    from vibe import internal_client, ui_server

    service, _, _ = service_for(state)
    monkeypatch.setattr(internal_client, "live_activity_request", service.handle)
    client = ui_server.app.test_client()
    headers = csrf_headers(client)
    base = "/api/web-push/live-activities"
    assert client.get(f"{base}/turn?session_id=missing").status_code == 404
    assert client.get(f"{base}/turn").status_code == 400
    for payload in [
        [],
        {},
        {**BODY, "extra": "name"},
        {**BODY, "activity_token": "UPPERCASE"},
        {**BODY, "push_registration_id": []},
    ]:
        assert client.put(f"{base}/valid", json=payload, headers=headers).status_code == 400
    assert client.delete(f"{base}/valid", json={"reason": []}, headers=headers).status_code == 400
    assert client.put(f"{base}/valid", json=BODY).status_code == 403

    async def unavailable(*args, **kwargs):
        raise internal_client.InternalServerUnavailable("offline")

    monkeypatch.setattr(internal_client, "live_activity_request", unavailable)
    assert client.get(f"{base}/turn?session_id={SESSION}").status_code == 503


def test_relay_requests_use_frozen_vectors_and_do_not_expose_registration(caplog):
    import json
    import logging
    from pathlib import Path
    import httpx
    from core.web_push import send_live_activity_update
    from tests.test_web_push_service import _test_badge_vapid_keys

    vectors = json.loads((Path(__file__).parent / "fixtures/live_activity/contract_vectors.json").read_text())
    caplog.set_level(logging.INFO, logger="httpx")
    seen = []

    def relay(request):
        seen.append(request)
        return httpx.Response(201, json={"ok": True, "registration_id": REGISTRATION, "apns_status": 200})

    with httpx.Client(transport=httpx.MockTransport(relay)) as client:
        for case in vectors["relay_cases"]:
            send_live_activity_update(
                subscription={"endpoint": f"https://avibe.bot/api/push/apns/{REGISTRATION}"},
                body=case["request"],
                vapid_keys=_test_badge_vapid_keys(),
                client=client,
            )
            assert json.loads(seen[-1].content) == case["request"]
            assert seen[-1].headers["authorization"].startswith("vapid t=")
    assert REGISTRATION not in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_current_project_and_remote_authority_are_rechecked_before_sending(state, monkeypatch):
    from core import web_push_notifications
    from storage import project_access_service
    from tests.test_web_push_notifications import _paired_revision_config
    from vibe.authorization import AuthorizationContext

    _paired_revision_config(41, instance_kind="organization")
    service, now, sent = service_for(state)
    owner = "remote:reader"
    context = AuthorizationContext(
        instance_role="viewer",
        subject="reader",
        email="member@example.com",
        instance_access_source="email",
        organization_id="org_1",
        organization_member_id="member_1",
        organization_role="member",
        instance_kind="organization",
        authorization_revision=41,
        claims_issued_at=now[0],
        is_remote=True,
    )
    with state.begin() as conn:
        conn.execute(web_push_subscriptions.update().values(user_key=owner))
        assert (
            project_access_service.apply_project_access_intent(
                conn,
                {
                    "project_id": "proj_last_reply",
                    "revision": 1,
                    "mode": "restricted",
                    "bindings": [
                        {"principal_kind": "email", "principal_value": "member@example.com", "access_role": "viewer"}
                    ],
                },
            ).outcome
            == "applied"
        )
    record = web_push_notifications.web_push_authorization_context_record(owner, context)
    await service.handle("put", owner, "activity-1", BODY, authorization=record)
    service.pending_question = lambda sid: True
    await service.tick()
    assert len(sent) == 1
    with state.begin() as conn:
        assert (
            project_access_service.apply_project_access_intent(
                conn,
                {
                    "project_id": "proj_last_reply",
                    "revision": 2,
                    "mode": "restricted",
                    "bindings": [],
                },
            ).outcome
            == "applied"
        )
    service.pending_question = lambda sid: False
    await service.tick()
    assert len(sent) == 1
    assert (await service.handle("get", owner, "activity-1", {}))["body"]["activity"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_internal_http_reaches_the_controller_owner_with_real_storage(state):
    import httpx
    from core import internal_server
    from tests.test_internal_server import _build_controller_double

    controller = _build_controller_double()
    service, _, sent = service_for(state)
    controller.live_activity_service = service
    app = internal_server.create_app(controller, instance_id="test-instance", bearer_token="test-bearer")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"operation": "put", "owner": "local", "activity_id": "activity-1", "payload": BODY}
        assert (await client.post("/internal/live-activities", json=body)).status_code == 401
        response = await client.post(
            "/internal/live-activities", json=body, headers={"Authorization": "Bearer test-bearer"}
        )
        assert response.status_code == 200
        assert response.json()["body"]["activity"]["status"] == "tracking"
        assert sent == []


def test_public_http_session_access_is_404_for_a_viewer_outside_the_project(state, monkeypatch):
    from storage import project_access_service
    from vibe import internal_client, ui_server
    from vibe.authorization import AuthorizationContext
    from vibe.ui_compat import g

    service, _, _ = service_for(state)
    monkeypatch.setattr(internal_client, "live_activity_request", service.handle)
    with state.begin() as conn:
        assert (
            project_access_service.apply_project_access_intent(
                conn,
                {
                    "project_id": "proj_last_reply",
                    "revision": 1,
                    "mode": "restricted",
                    "bindings": [],
                },
            ).outcome
            == "applied"
        )
    context = AuthorizationContext(instance_role="viewer", subject="reader", is_remote=True)
    # The HTTP auth layer has already resolved the caller; test its session-read boundary.
    original = ui_server._live_activity_http

    async def as_viewer(*args, **kwargs):
        g.authorization_context = context
        return await original(*args, **kwargs)

    monkeypatch.setattr(ui_server, "_live_activity_http", as_viewer)
    client = ui_server.app.test_client()
    headers = csrf_headers(client)
    assert client.get(f"/api/web-push/live-activities/turn?session_id={SESSION}").status_code == 404
    assert client.put("/api/web-push/live-activities/activity-1", json=BODY, headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_a_terminal_outage_keeps_one_pending_end_until_a_later_heartbeat_accepts_it(state):
    import httpx
    from storage import live_activity_service as tracking_store

    service, now, sent = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)
    with state.begin() as conn:
        message_deliveries.terminalize_turn(
            conn, TURN, outcome="completed", settled_by="terminal_result", evidence_kind="test"
        )

    def unavailable(**kwargs):
        sent.append(kwargs["body"])
        raise httpx.ReadTimeout("no answer")

    service.sender = unavailable
    for _ in range(4):
        await service.tick()
        now[0] += 5
    with state.connect() as conn:
        row = tracking_store.get(conn, "activity-1")
        assert row["pending_state"] is not None
        assert row["status"] == "tracking"
    assert len(sent) == 4
    await service.tick()
    assert len(sent) == 4
    now[0] += 900
    service.sender = lambda **kwargs: sent.append(kwargs["body"])
    await service.tick()
    assert len(sent) == 5
    assert all(body["event"] == "end" for body in sent)
    # Lost responses permit duplicate wire delivery, but never a second terminal state.
    assert len({(body["phase"], body["started_at"], body["ended_at"]) for body in sent}) == 1
    assert all(left["timestamp"] < right["timestamp"] for left, right in zip(sent, sent[1:]))
    assert sent[-1]["confirmed_at"] == now[0]
    with state.connect() as conn:
        row = tracking_store.get(conn, "activity-1")
        assert row["pending_state"] is None
        assert row["status"] == "ended"
    await service.tick()
    assert len(sent) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("source_gone", [True, False])
async def test_a_new_phase_replaces_pending_work_and_terminal_pending_survives_restart(state, monkeypatch, source_gone):
    import httpx
    import json
    from storage import live_activity_service as tracking_store

    service, now, sent = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)

    def unavailable(**kwargs):
        sent.append(kwargs["body"])
        raise httpx.ReadTimeout("no answer")

    service.sender = unavailable
    service.pending_question = lambda sid: True
    await service.tick()
    service.pending_question = lambda sid: False
    await service.tick()
    with state.connect() as conn:
        row = tracking_store.get(conn, "activity-1")
        assert json.loads(row["pending_state"])["phase"] == "working"
    with state.begin() as conn:
        message_deliveries.terminalize_turn(
            conn, TURN, outcome="completed", settled_by="terminal_result", evidence_kind="test"
        )
    await service.tick()
    result = await service.handle("get", "local", "activity-1", {})
    assert result["body"]["activity"]["phase"] == "done"
    assert result["body"]["activity"]["status"] == "tracking"
    saved_end = dict(sent[-1])
    restarted, _, _ = service_for(state)
    restarted.clock = service.clock
    restarted.sender = lambda **kwargs: sent.append(kwargs["body"])
    now[0] += 5
    # Source deletion is a stop rule, even with an end pending.
    if source_gone:
        from storage.models import message_deliveries as deliveries
        with state.begin() as conn:
            conn.execute(deliveries.delete().where(deliveries.c.turn_id == TURN))
            conn.execute(session_turns.delete().where(session_turns.c.id == TURN))
        count = len(sent)
        await restarted.tick()
        assert len(sent) == count
        with state.connect() as conn:
            row = tracking_store.get(conn, "activity-1")
            assert row["status"] == "stopped" and row["stop_reason"] == "turn_unavailable"
            assert row["pending_state"] is None
        return
    # A changed source cannot overwrite a terminal state already attempted.
    next_state = {
        "phase": "failed", "started_at": saved_end["started_at"] + 1,
        "ended_at": saved_end["ended_at"] + 1, "confirmed_at": now[0], "turn_id": TURN,
    }
    monkeypatch.setattr(tracking_store, "snapshot", lambda *args: next_state)
    await restarted.tick()
    assert sent[-1]["event"] == "end"
    assert all(sent[-1][key] == saved_end[key] for key in ("phase", "started_at", "ended_at"))
    assert sent[-1]["timestamp"] > saved_end["timestamp"]
    assert (await restarted.handle("get", "local", "activity-1", {}))["body"]["activity"]["status"] == "ended"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 502])
async def test_relay_rejection_stops_tracking_without_affecting_device_notifications(state, status):
    import httpx
    from storage import live_activity_service as tracking_store

    service, now, _ = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)

    def rejected(**kwargs):
        response = httpx.Response(status, request=httpx.Request("POST", "https://avibe.bot"))
        raise httpx.HTTPStatusError("rejected", request=response.request, response=response)

    service.sender = rejected
    service.pending_question = lambda sid: True
    await service.tick()
    with state.connect() as conn:
        row = tracking_store.get(conn, "activity-1")
        assert row["status"] == "stopped"
        assert row["stop_reason"] == "relay_rejected"
        assert row["pending_state"] is None
        assert conn.execute(select(web_push_subscriptions.c.enabled)).scalar_one() == 1


@pytest.mark.asyncio
async def test_restart_without_pending_state_waits_for_a_new_heartbeat_period(state):
    service, now, sent = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)
    now[0] += 1800
    from core.live_activities import LiveActivityService

    restarted = LiveActivityService(engine=state, clock=service.clock, sender=service.sender)
    await restarted.tick()
    assert sent == []
    now[0] += 900
    await restarted.tick()
    assert len(sent) == 1 and sent[0]["priority"] == 5


def test_public_http_requested_time_anchors_expiry_and_is_immutable(state, monkeypatch):
    from vibe import internal_client, ui_server
    from storage import live_activity_service as tracking_store

    service, now, _ = service_for(state)
    monkeypatch.setattr(internal_client, "live_activity_request", service.handle)
    client = ui_server.app.test_client()
    headers = csrf_headers(client)
    url = "/api/web-push/live-activities/delayed"
    body = {**BODY, "requested_at": now[0] - 7200}
    assert client.put(url, json=body, headers=headers).status_code == 200
    with state.connect() as conn:
        row = tracking_store.get(conn, "delayed")
        assert row["created_at"] == now[0]
        assert row["requested_at"] == body["requested_at"]
        assert row["expires_at"] == body["requested_at"] + 28800
    conflict = client.put(url, json={**body, "requested_at": body["requested_at"] + 1}, headers=headers)
    assert conflict.status_code == 409 and conflict.get_json()["error"] == "activity_conflict"
    for invalid in [True, 1.5, "1795000031", None, now[0] + 301]:
        response = client.put(url, json={**body, "requested_at": invalid}, headers=headers)
        assert response.status_code == 400
    now[0] = body["requested_at"] + 28800
    for target in [url, "/api/web-push/live-activities/expired-new"]:
        response = client.put(target, json=body, headers=headers)
        assert response.status_code == 409 and response.get_json()["error"] == "activity_expired"
    with state.connect() as conn:
        assert tracking_store.get(conn, "delayed")["stop_reason"] == "expired"
        assert tracking_store.get(conn, "expired-new") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["activity_token_gone", "registration_gone", None])
@pytest.mark.parametrize("terminal", [False, True])
async def test_only_a_new_token_can_revive_token_gone_with_its_pending_state(state, error, terminal):
    import httpx
    from storage import live_activity_service as tracking_store

    service, now, sent = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)
    service.pending_question = lambda sid: True

    def gone(**kwargs):
        response = httpx.Response(410, json={"error": error}, request=httpx.Request("POST", "https://avibe.bot"))
        raise httpx.HTTPStatusError("gone", request=response.request, response=response)

    service.sender = gone
    await service.tick()
    with state.connect() as conn:
        row = tracking_store.get(conn, "activity-1")
        assert row["stop_reason"] == ("token_gone" if error == "activity_token_gone" else "registration_gone")
        assert row["status"] == "stopped"
        pending_state = row["pending_state"]
        assert bool(pending_state) is (error == "activity_token_gone")
    same = await service.handle("put", "local", "activity-1", BODY)
    assert same["body"]["activity"]["status"] == "stopped"
    if terminal:
        with state.begin() as conn:
            message_deliveries.terminalize_turn(
                conn, TURN, outcome="completed", settled_by="terminal_result", evidence_kind="test"
            )
    fresh = await service.handle("put", "local", "activity-1", {**BODY, "activity_token": "cd" * 32})
    revived = error == "activity_token_gone" and not terminal
    assert fresh["body"]["activity"]["status"] == ("tracking" if revived else "stopped")
    with state.connect() as conn:
        row = tracking_store.get(conn, "activity-1")
        assert row["pending_state"] == pending_state
        assert row["expires_at"] == BODY["requested_at"] + 28800
        if revived:
            assert row["stop_reason"] is None
    service.sender = lambda **kwargs: sent.append(kwargs["body"])
    await service.tick()
    assert len(sent) == int(revived)
    if revived:
        assert sent[0]["activity_token"] == "cd" * 32
        assert sent[0]["phase"] == "needsYou"


@pytest.mark.asyncio
async def test_terminal_put_settles_pending_end_locally_without_another_push(state):
    import httpx
    from storage import live_activity_service as tracking_store

    service, now, sent = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)
    with state.begin() as conn:
        message_deliveries.terminalize_turn(
            conn, TURN, outcome="completed", settled_by="terminal_result", evidence_kind="test"
        )

    def unavailable(**kwargs):
        sent.append(kwargs["body"])
        raise httpx.ReadTimeout("no answer")

    service.sender = unavailable
    await service.tick()
    response = await service.handle("put", "local", "activity-1", BODY)
    assert response["status_code"] == 409 and response["body"]["error"] == "turn_not_active"
    assert response["body"]["activity"]["status"] == "ended"
    with state.connect() as conn:
        row = tracking_store.get(conn, "activity-1")
        assert row["status"] == "ended" and row["pending_state"] is None
    now[0] += 900
    await service.tick()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_pause_keeps_enrollment_and_resumes_terminal_delivery(state, monkeypatch):
    from core import web_push_notifications as notifications
    from storage.models import device_function_settings as settings, remote_device_push_links as links
    from types import SimpleNamespace

    owner = "remote:reader"
    device = dict(binding="test", subject="reader", device_id="phone")
    with state.begin() as conn:
        conn.execute(web_push_subscriptions.update().values(user_key=owner, device_id="push"))
        sub_id = conn.scalar(select(web_push_subscriptions.c.id))
        conn.execute(links.insert().values(**device, subscription_id=sub_id, email="test@example.invalid",
                                           login_role="viewer", push_id="push", session_id="signed-in"))
        conn.execute(settings.insert().values(**device, notifications_on=1))
    monkeypatch.setattr(notifications, "_resolve_owner_authorization_decisions", lambda *a, **kw: {
        owner: SimpleNamespace(authorized=True, context=None)
    })
    service, _, sent = service_for(state)
    result = await service.handle("put", owner, "activity-1", BODY)
    assert result["body"]["activity"]["status"] == "tracking"
    with state.begin() as conn:
        conn.execute(settings.update().values(notifications_on=0))
    service.pending_question = lambda sid: True
    await service.tick()
    assert sent == []
    assert service._read("activity-1")["status"] == "tracking"
    with state.begin() as conn:
        message_deliveries.terminalize_turn(conn, TURN, outcome="completed", settled_by="terminal_result", evidence_kind="test")
    await service.tick()
    assert sent == []
    with state.begin() as conn:
        conn.execute(settings.update().values(notifications_on=1))
    await service.tick()
    await service.tick()
    assert [body["event"] for body in sent] == ["end"]
    assert service._read("activity-1")["status"] == "ended"


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [True, False])
async def test_confirmed_owner_revocation_stops_but_authority_outage_only_defers(state, monkeypatch, revoked):
    from core import web_push_notifications as notifications
    from storage import live_activity_service as tracking_store

    service, _, sent = service_for(state)
    owner = "remote:reader"
    with state.begin() as conn:
        conn.execute(web_push_subscriptions.update().values(user_key=owner))
    await service.handle("put", owner, "activity-1", BODY)
    decision = notifications.OwnerAuthorizationDecision(
        user_key=owner, policy="organization", context=None, authorized=False,
        disposition=notifications.WEB_PUSH_DISPOSITION_REVOKED if revoked
        else notifications.WEB_PUSH_DISPOSITION_CONFIG_UNAVAILABLE,
        reason="test",
    )
    monkeypatch.setattr(notifications, "_resolve_owner_authorization_decisions", lambda *args, **kwargs: {owner: decision})
    service.pending_question = lambda sid: True
    await service.tick()
    assert sent == []
    with state.connect() as conn:
        row = tracking_store.get(conn, "activity-1")
        assert row["status"] == ("stopped" if revoked else "tracking")
        assert row["stop_reason"] == ("session_unavailable" if revoked else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["registration", "session"])
async def test_missing_local_source_permanently_stops_delivery(state, source):
    from storage import live_activity_service as tracking_store
    from storage.models import agent_sessions, message_deliveries as deliveries

    service, _, sent = service_for(state)
    await service.handle("put", "local", "activity-1", BODY)
    with state.begin() as conn:
        if source == "registration":
            web_push_service.disable_subscription(conn, endpoint=f"https://avibe.bot/api/push/apns/{REGISTRATION}")
        else:
            conn.execute(deliveries.delete().where(deliveries.c.turn_id == TURN))
            conn.execute(session_turns.delete().where(session_turns.c.id == TURN))
            conn.execute(agent_sessions.delete().where(agent_sessions.c.id == SESSION))
    service.pending_question = lambda sid: True
    await service.tick()
    assert sent == []
    with state.connect() as conn:
        row = tracking_store.get(conn, "activity-1")
        assert row["status"] == "stopped"
        assert row["stop_reason"] == f"{source}_unavailable"
