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
BODY = {"session_id": SESSION, "turn_id": TURN, "push_registration_id": REGISTRATION, "activity_token": TOKEN}


@pytest.fixture
def state():
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


def test_public_http_register_rotate_end_and_owner_isolation(state, monkeypatch):
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
    assert client.delete(f"{base}/activity-1", json={"reason": "instance_switch"}, headers=headers).status_code == 200
    assert client.get(f"{base}/activity-1").get_json()["activity"]["status"] == "stopped"
    assert client.put(f"{base}/activity-1", json=BODY, headers=headers).get_json()["activity"]["status"] == "stopped"


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
async def test_failed_heartbeat_is_not_retried(state):
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
    assert len(sent) == 1 and sent[0]["priority"] == 5


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
