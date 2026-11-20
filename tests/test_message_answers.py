"""MESSAGE-DELIVERY-340: D2 authority through HTTP, SQLite and controller admission."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading

import httpx
import pytest
from sqlalchemy import select

from storage import messages_service
from storage.db import create_sqlite_engine
from storage.models import message_deliveries
from tests.test_ui_session_stream import _make_session, isolated_state  # noqa: F401
from tests.ui_server_test_helpers import csrf_headers


def revision(options):
    return hashlib.sha256(json.dumps(options, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def answer_route(isolated_state, tmp_path, monkeypatch):
    from core import internal_server
    from tests.test_internal_server import _bind_test_native_start, _build_controller_double
    from vibe.ui_server import app

    scope, session = _make_session(tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        question = messages_service.append(
            conn,
            scope_id=scope,
            session_id=session,
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="Continue?",
            content={"kind": "result", "quick_replies": ["继续 ✅", "Stop"]},
        )
    controller = _build_controller_double()
    internal_app = internal_server.create_app(controller)
    manager = controller.session_turns
    starts, dispatches = [], []

    async def run(sid, context, text, *, logical_turn_id=None, **_kwargs):
        starts.append(text)
        _bind_test_native_start(engine, context)
        manager._terminalize_durable_turn(
            logical_turn_id,
            "completed",
            settled_by="terminal_result",
            evidence_kind="test_terminal",
        )

    manager._run = run
    entered, release = threading.Event(), threading.Event()
    release.set()

    async def dispatch(payload):
        dispatches.append(payload)
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://testserver"
        ) as client:
            response = await client.post("/internal/dispatch_async", json=payload)
        return {"status_code": response.status_code, "body": response.json()}

    monkeypatch.setattr("vibe.internal_client.dispatch_async", dispatch)

    def post(operation="op-one", *, text="继续 ✅", free=False, legacy=False, **extra):
        body = {"text": text}
        if free:
            body["in_reply_to"] = question["id"]
        else:
            body["metadata"] = {"quick_reply_for": question["id"]}
        if not legacy:
            body.update(operation_id=operation, revision=revision(["继续 ✅", "Stop"]))
        body.update(extra)
        client = app.test_client()
        return client.post(f"/api/sessions/{session}/messages", json=body, headers=csrf_headers(client))

    return dict(
        post=post,
        engine=engine,
        session=session,
        scope=scope,
        question=question,
        starts=starts,
        dispatches=dispatches,
        entered=entered,
        release=release,
    )


@pytest.mark.parametrize("second", ["choice", "text", "legacy", "replay"])
def test_concurrent_answers_reserve_before_dispatch(answer_route, second):
    """MESSAGE-DELIVERY-340: concurrent surfaces reserve one answer before dispatch."""
    r = answer_route
    r["release"].clear()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(r["post"])
        assert r["entered"].wait(5)
        try:
            another = executor.submit(
                r["post"],
                "op-one" if second == "replay" else "op-two",
                free=second == "text",
                legacy=second == "legacy",
            )
            # The losing request must finish while the first IPC is still blocked.
            response = another.result(timeout=3)
            assert response.get_json()["answer"]["outcome"] == (
                "duplicate" if second == "replay" else "already_handled"
            )
        finally:
            r["release"].set()
        assert first.result(timeout=10).status_code in (201, 202)
    assert len(r["dispatches"]) == len(r["starts"]) == 1
    with r["engine"].connect() as conn:
        assert (
            len(conn.execute(select(message_deliveries).where(message_deliveries.c.session_id == r["session"])).all())
            == 1
        )


def test_operation_replay_is_durable_and_bound_to_input(answer_route):
    """MESSAGE-DELIVERY-340: restart-safe replay keeps the original input and receipt."""
    r = answer_route
    first = r["post"]()
    assert first.get_json()["answer"]["outcome"] == "accepted"
    r["engine"].dispose()
    replay = r["post"]()
    assert replay.get_json()["answer"]["outcome"] == "duplicate"
    assert replay.get_json()["answer"]["original_outcome"] == "accepted"
    changed = r["post"](text="Stop")
    assert changed.status_code == 409
    assert changed.get_json()["error"]["code"] == "operation_conflict"
    assert len(r["dispatches"]) == len(r["starts"]) == 1


@pytest.mark.parametrize("cause", ["revision", "user", "agent", "pending", "missing", "archived"])
def test_stale_and_missing_answers_do_not_dispatch(answer_route, cause):
    """MESSAGE-DELIVERY-340: stale or missing native answer targets never dispatch."""
    from sqlalchemy import update
    from storage.models import agent_sessions
    from core.input_admission import InputEnvelope, InputProvenance, InputSender, admit_input

    r = answer_route
    extra = {}
    with r["engine"].begin() as conn:
        if cause in ("user", "agent"):
            messages_service.append(
                conn,
                scope_id=r["scope"],
                session_id=r["session"],
                platform="avibe",
                author=cause,
                source=cause,
                message_type="user" if cause == "user" else "result",
                text="New conversation",
            )
        if cause == "pending":
            admit_input(
                conn,
                InputEnvelope(
                    session_id=r["session"],
                    scope_id=r["scope"],
                    sender=InputSender("user"),
                    kind="user",
                    provenance=InputProvenance("web_composer"),
                    dispatch_text="Unsent input",
                    text="Unsent input",
                ),
                delivery_id="delivery-newer-input",
            )
        if cause == "archived":
            conn.execute(update(agent_sessions).where(agent_sessions.c.id == r["session"]).values(status="archived"))
    if cause == "revision":
        extra["revision"] = "0" * 64
    if cause == "missing":
        extra["metadata"] = {"quick_reply_for": "missing"}
    response = r["post"](**extra)
    assert response.status_code == (404 if cause == "missing" else 409)
    assert response.get_json()["answer"]["outcome"] == ("not_found" if cause == "missing" else "stale")
    assert r["dispatches"] == []


def test_queued_input_accepted_after_question_makes_answer_stale(answer_route, monkeypatch):
    from datetime import datetime, timedelta
    from storage import message_deliveries as deliveries

    r = answer_route
    question_at = datetime.fromisoformat(r["question"]["created_at"].replace("Z", "+00:00"))
    submitted_at = (question_at - timedelta(seconds=1)).isoformat()
    accepted_at = (question_at + timedelta(seconds=1)).isoformat()
    with r["engine"].begin() as conn:
        queued = deliveries.enqueue_queued(
            conn, scope_id=r["scope"], session_id=r["session"], text="Queued before question", now=submitted_at
        )
        claimed = deliveries.claim_start_batch(
            conn,
            turn_id="turn-queued-input",
            session_id=r["session"],
            backend="codex",
            deliveries=[deliveries.get_delivery(conn, queued["id"])],
            dispatch_text="Queued before question",
        )
        turn = claimed["turn"]
        assert deliveries.bind_native_start(
            conn,
            turn["id"],
            expected_version=turn["version"],
            runtime_key="runtime-queued-input",
            runtime_turn_id="runtime-turn-queued-input",
            native_turn_id="native-queued-input",
        )
        monkeypatch.setattr(deliveries, "turn_now_iso", lambda: accepted_at)
        accepted = deliveries.materialize_start_acceptance(
            conn, turn_id=turn["id"], evidence={"kind": "test_native_acceptance"}
        )
        assert accepted[0]["state"] == "accepted"
        message = messages_service.get_message(conn, accepted[0]["message_id"], session_id=r["session"])
        assert message["created_at"] < r["question"]["created_at"] < message["delivered_at"]
        transcript = messages_service.list_session_messages(conn, session_id=r["session"])["messages"]
        assert [row["id"] for row in transcript] == [r["question"]["id"], message["id"]]

    response = r["post"]()
    assert response.status_code == 409
    assert response.get_json()["answer"]["outcome"] == "stale"
    assert r["dispatches"] == []


@pytest.mark.parametrize("state", ["queued", "retired", "claimed_own", "claimed_other", "reserved", "legacy_queued"])
def test_older_input_freshness_follows_acceptance_lifecycle(answer_route, state):
    """MESSAGE-DELIVERY-340: older live work precedes answers except Q's own start."""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    from storage import message_deliveries as deliveries
    from storage.models import messages

    r = answer_route
    question_at = datetime.fromisoformat(r["question"]["created_at"].replace("Z", "+00:00"))
    submitted_at = (question_at - timedelta(seconds=1)).isoformat()
    with r["engine"].begin() as conn:
        conn.execute(messages.update().where(messages.c.id == r["question"]["id"]).values(
            metadata_json=func.json_set(messages.c.metadata_json, "$.turn_id", "question-turn"),
        ))
        if state == "reserved":
            deliveries.insert_delivery(
                conn, delivery_id="older-reserved", session_id=r["session"], priority="p1", state="reserved",
                snapshot=deliveries.message_snapshot(
                    scope_id=r["scope"], session_id=r["session"], platform="avibe",
                    author="user", source="user", text="Earlier input",
                ),
                dispatch_text="Earlier input", now=submitted_at,
            )
        else:
            queued = deliveries.enqueue_queued(
                conn, scope_id=r["scope"], session_id=r["session"], text="Earlier input", now=submitted_at,
            )
            if state == "retired":
                assert deliveries.retire_queued(conn, r["session"], queued["id"])
            elif state.startswith("claimed_"):
                deliveries.claim_start_batch(
                    conn, turn_id="question-turn" if state == "claimed_own" else "other-turn",
                    session_id=r["session"], backend="codex",
                    deliveries=[deliveries.get_delivery(conn, queued["id"])], dispatch_text="Earlier input",
                )
    response = r["post"](legacy=state == "legacy_queued")
    fresh = state in {"retired", "claimed_own", "legacy_queued"}
    assert response.status_code in ((201, 202) if fresh else (409,))
    assert response.get_json()["answer"]["outcome"] == ("accepted" if fresh else "stale")
    assert len(r["dispatches"]) == int(fresh)


def test_answer_freshness_queries_are_bounded_by_live_work(answer_route, monkeypatch):
    """MESSAGE-DELIVERY-340: live-state ranges exclude accepted and retired history."""
    from datetime import datetime, timedelta
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from storage import message_deliveries as deliveries

    r = answer_route
    question_at = datetime.fromisoformat(r["question"]["created_at"].replace("Z", "+00:00"))
    submitted_at = (question_at - timedelta(seconds=2)).isoformat()
    accepted_at = (question_at - timedelta(seconds=1)).isoformat()
    with r["engine"].begin() as conn:
        for number in range(250):
            queued = deliveries.enqueue_queued(
                conn, scope_id=r["scope"], session_id=r["session"], text=f"Retired input {number}", now=submitted_at,
            )
            assert deliveries.retire_queued(conn, r["session"], queued["id"])
        history = [
            deliveries.enqueue_queued(
                conn, scope_id=r["scope"], session_id=r["session"], text=f"Accepted input {number}", now=submitted_at,
            )
            for number in range(250)
        ]
        claimed = deliveries.claim_start_batch(
            conn, turn_id="history-turn", session_id=r["session"], backend="codex",
            deliveries=[deliveries.get_delivery(conn, row["id"]) for row in history], dispatch_text="Historical input",
        )
        turn = claimed["turn"]
        assert deliveries.bind_native_start(
            conn, turn["id"], expected_version=turn["version"], runtime_key="history-runtime",
            runtime_turn_id="history-runtime-turn", native_turn_id="history-native-turn",
        )
        monkeypatch.setattr(deliveries, "turn_now_iso", lambda: accepted_at)
        assert len(deliveries.materialize_start_acceptance(
            conn, turn_id=turn["id"], evidence={"kind": "test_native_acceptance"},
        )) == 250
        deliveries.enqueue_queued(
            conn, scope_id=r["scope"], session_id=r["session"], text="Still pending", now=submitted_at,
        )
    plans = {}

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT message_deliveries.id") and "json_extract" in statement:
            key = "pending"
        elif statement.startswith("SELECT messages.id") and "messages.author IN" in statement:
            key = "transcript"
        else:
            return
        plans[key] = "\n".join(
            row[3] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
        )

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        assert r["post"]().get_json()["answer"]["outcome"] == "stale"
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
    assert "ix_message_deliveries_session_state (session_id=? AND state=?)" in plans["pending"], plans
    assert "submitted_at>?" not in plans["pending"], plans
    assert "session_id=? AND <expr>>?" in plans["transcript"], plans
    assert all("SCAN " not in plan and "TEMP B-TREE" not in plan for plan in plans.values()), plans


def test_text_answer_is_ordinary_input_and_preserves_composer_draft(answer_route):
    from storage import message_deliveries as deliveries

    r = answer_route
    with r["engine"].begin() as conn:
        deliveries.set_draft(conn, r["session"], "Still editing")
    response = r["post"](free=True, text="稍后，我再想想")
    assert response.get_json()["answer"]["outcome"] == "accepted"
    assert response.get_json()["draft_advanced"] is False
    assert response.get_json()["draft"]["text"] == "Still editing"
    assert r["dispatches"][0]["message_kind"] == "original"
    assert "quick_reply_for" not in r["dispatches"][0]["metadata"]
    assert r["post"]("another").get_json()["answer"]["outcome"] == "already_handled"


@pytest.mark.parametrize(
    "vector",
    json.loads((Path(__file__).parent / "fixtures/message_answer_contract_vectors.json").read_text())["answerKinds"],
    ids=lambda vector: vector["answer_kind"],
)
def test_answer_preserves_committed_vibey_context_and_receipt_kind(answer_route, vector):
    from storage.agent_transcript import SQLiteTranscriptStore
    from storage.models import messages
    from tests.test_agent_transcript import _assistant, _user

    r = answer_route
    free = vector["answer_kind"] == "text"
    store = SQLiteTranscriptStore(r["engine"])
    with r["engine"].begin() as conn:
        first = messages_service.append(
            conn, scope_id=r["scope"], session_id=r["session"], platform="avibe",
            author="user", source="user", message_type="user", text="Start",
        )
    asyncio.run(store.consume_input(r["session"], first["id"], _user("Start")))
    entry = asyncio.run(store.append_response(r["session"], _assistant("Continue?"), final=True))
    with r["engine"].begin() as conn:
        raw = json.loads(conn.execute(
            select(messages.c.content_json).where(messages.c.id == entry.row_id)
        ).scalar_one())
        raw["quick_replies"] = ["继续 ✅", "Stop"]
        conn.execute(messages.update().where(messages.c.id == entry.row_id).values(content_json=json.dumps(raw)))
        r["question"].update(messages_service.get_message(conn, entry.row_id, session_id=r["session"]))
    expected_context = asyncio.run(store.load(r["session"]))
    assert expected_context
    response = r["post"](free=free).get_json()["answer"]
    assert asyncio.run(store.load(r["session"])) == expected_context
    with r["engine"].connect() as conn:
        saved = json.loads(conn.execute(
            select(messages.c.content_json).where(messages.c.id == entry.row_id)
        ).scalar_one())
    assert saved["model"] == raw["model"]
    assert "quick_reply_revision" not in saved
    assert "failure_source" not in saved
    assert saved["quick_reply_chosen"] == vector["storedChoice"]
    assert saved["quick_reply_answer_kind"] == vector["answer_kind"]
    for receipt in (
        response,
        r["post"](free=free).get_json()["answer"],
        r["post"]("another", free=not free).get_json()["answer"],
    ):
        assert receipt["answer_kind"] == vector["answer_kind"]
        assert receipt["choice"] == vector["choice"]


@pytest.mark.parametrize("newer", ["agent", "user", "pending"])
def test_legacy_can_answer_an_older_unanswered_group(answer_route, newer):
    from storage import message_deliveries as deliveries

    r = answer_route
    with r["engine"].begin() as conn:
        if newer == "pending":
            deliveries.enqueue_queued(conn, scope_id=r["scope"], session_id=r["session"], text="New input")
        else:
            messages_service.append(
                conn, scope_id=r["scope"], session_id=r["session"], platform="avibe",
                author=newer, source=newer, message_type="user" if newer == "user" else "result", text="Newer input",
            )
    response = r["post"](legacy=True)
    assert response.status_code in (201, 202)
    assert response.get_json()["answer"]["outcome"] == "accepted"
    assert len(r["dispatches"]) == 1


def test_legacy_archived_response_preserves_existing_error_code(answer_route):
    from storage.models import agent_sessions

    r = answer_route
    with r["engine"].begin() as conn:
        conn.execute(agent_sessions.update().where(agent_sessions.c.id == r["session"]).values(status="archived"))
    response = r["post"](legacy=True)
    assert response.status_code == 409
    assert response.get_json()["code"] == "session_archived"
    assert "answer" not in response.get_json()
    assert r["dispatches"] == []


@pytest.mark.parametrize("cause", ["missing", "no_options", "non_option"])
def test_legacy_unmatched_quick_reply_keeps_original_dispatch_response(answer_route, cause):
    """MESSAGE-DELIVERY-340: legacy passthrough preserves the unrelated composer draft."""
    from storage import message_deliveries as deliveries
    from storage.models import messages, message_answer_operations

    r = answer_route
    with r["engine"].begin() as conn:
        deliveries.set_draft(conn, r["session"], "Still editing")
        if cause == "missing":
            conn.execute(messages.delete().where(messages.c.id == r["question"]["id"]))
        if cause == "no_options":
            conn.execute(messages.update().where(messages.c.id == r["question"]["id"]).values(content_json="{}"))
    response = r["post"](legacy=True, text="Other input" if cause == "non_option" else "继续 ✅")
    assert response.status_code in (201, 202)
    assert "answer" not in response.get_json()
    assert response.get_json()["draft_advanced"] is False
    assert response.get_json()["draft"]["text"] == "Still editing"
    assert len(r["dispatches"]) == 1
    assert r["dispatches"][0]["message_kind"] == "quick_reply"
    assert r["dispatches"][0]["metadata"]["quick_reply_for"] == r["question"]["id"]
    with r["engine"].connect() as conn:
        assert deliveries.get_draft_state(conn, r["session"])["text"] == "Still editing"
        assert conn.execute(select(message_answer_operations)).first() is None
        target = messages_service.get_message(conn, r["question"]["id"], session_id=r["session"])
    assert target is None or "quick_reply_chosen" not in target["content"]


def test_ordinary_composer_send_advances_draft(answer_route):
    """MESSAGE-DELIVERY-340: only an ordinary composer send advances its draft."""
    from storage import message_deliveries as deliveries

    r = answer_route
    with r["engine"].begin() as conn:
        deliveries.set_draft(conn, r["session"], "Still editing")
    response = r["post"](legacy=True, metadata={}, text="Composer message")
    assert response.status_code in (201, 202)
    assert "answer" not in response.get_json()
    assert response.get_json()["draft_advanced"] is True
    assert response.get_json()["draft"]["text"] == ""
    with r["engine"].connect() as conn:
        assert deliveries.get_draft_state(conn, r["session"])["text"] == ""


def test_winner_lookup_excludes_refusal_history_and_legacy_refusals_are_not_saved(answer_route):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from storage.models import message_answer_operations as operations

    r = answer_route
    with r["engine"].begin() as conn:
        conn.execute(operations.insert(), [
            dict(session_id=r["session"], principal="fixture", operation_id=f"refused-{number}",
                 request_id=r["question"]["id"], fingerprint="[]", choice="ignored",
                 receipt_json='{"outcome":"stale"}')
            for number in range(200)
        ])
    plans = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT ") and "JOIN message_deliveries" in statement:
            plans.extend(row[3] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters))

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        assert r["post"]().get_json()["answer"]["outcome"] == "accepted"
        assert r["post"](legacy=True).get_json()["already_answered"] is True
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
    with r["engine"].connect() as conn:
        assert len(conn.execute(select(operations)).all()) == 201
    assert any(
        "ix_message_answer_operations_winner (session_id=? AND request_id=? AND delivery_id>?)" in plan
        for plan in plans
    ), plans
    assert not any("SCAN " in plan for plan in plans), plans


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "lost_ack"])
def test_dispatch_failure_releases_only_proven_unaccepted_answers(answer_route, monkeypatch, failure):
    """MESSAGE-DELIVERY-340: uncertain acceptance retains exclusive answer ownership."""
    from vibe import internal_client

    r = answer_route
    real_dispatch = internal_client.dispatch_async

    async def failed(payload):
        if failure == "lost_ack":
            await real_dispatch(payload)
        if failure == "unavailable":
            raise internal_client.InternalServerUnavailable("not connected")
        raise internal_client.InternalServerTimeout("ack lost")

    monkeypatch.setattr(internal_client, "dispatch_async", failed)
    assert r["post"]().status_code in (502, 504)
    monkeypatch.setattr(internal_client, "dispatch_async", real_dispatch)
    replay = r["post"]().get_json()["answer"]
    assert replay["outcome"] == "duplicate"
    assert replay["original_outcome"] == ("accepted" if failure == "lost_ack" else None)
    contender = r["post"]("second")
    if failure == "unavailable":
        assert contender.get_json()["answer"]["outcome"] == "accepted"
    else:
        assert contender.get_json()["answer"]["outcome"] == "already_handled"
    assert len(r["starts"]) == (0 if failure == "timeout" else 1)


def test_question_revision_shared_vectors(answer_route):
    from pathlib import Path
    from core.message_answers import revision as runtime_revision

    vectors = json.loads((Path(__file__).parent / "fixtures/message_answer_contract_vectors.json").read_text())
    for question in vectors["questions"]:
        assert runtime_revision(question["options"]) == question["revision"]
    assert answer_route["question"]["content"]["quick_reply_revision"] == vectors["questions"][1]["revision"]


@pytest.mark.parametrize("legacy", [False, True])
def test_revoked_chat_permission_precedes_operation_replay(answer_route, tmp_path, legacy):
    """MESSAGE-DELIVERY-340: revoked access prevents dispatch and receipt disclosure."""
    import traceback
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from tests.ui_server_test_helpers import _save_config, remote_session_cookie
    from vibe import remote_access
    from vibe.ui_server import app

    r = answer_route
    config = _save_config(tmp_path, paired=True)
    client = app.test_client()
    body = {
        "text": "继续 ✅",
        "metadata": {"quick_reply_for": r["question"]["id"]},
        "operation_id": "same-operation",
        "revision": revision(["继续 ✅", "Stop"]),
    }
    if legacy:
        body.pop("operation_id")
        body.pop("revision")
    plans = {}

    def capture(conn, cursor, statement, parameters, context, executemany):
        if not statement.startswith("SELECT "):
            return
        callers = {frame.name for frame in traceback.extract_stack()}
        if "_persist_user_row" not in callers:
            return
        for caller in ("get_session", "ensure_session_agent_access", "_session_can_chat"):
            if caller in callers:
                plans.setdefault(caller, []).extend(
                    row[3] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
                )

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        for role, expected in (("owner", 201), ("viewer", 403)):
            client.set_cookie(
                remote_access.SESSION_COOKIE_NAME,
                remote_session_cookie(config, "owner@example.com", "same-user", role=role),
                domain="alex.avibe.bot",
            )
            response = client.post(
                f"/api/sessions/{r['session']}/messages",
                json=body,
                headers=csrf_headers(client, "https://alex.avibe.bot"),
                base_url="https://alex.avibe.bot",
                environ_base={"REMOTE_ADDR": "203.0.113.10"},
            )
            assert response.status_code == expected
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
    assert len(r["dispatches"]) == len(r["starts"]) == 1
    assert "answer" not in response.get_json()  # The outer auth guard discloses no saved receipt.
    assert set(plans) == {"get_session", "ensure_session_agent_access", "_session_can_chat"}
    assert all("SCAN " not in plan for query_plans in plans.values() for plan in query_plans), plans
    (tmp_path / "authorization-query-plans.json").write_text(json.dumps(plans, indent=2))


def test_answer_migration_preserves_existing_rows_and_epoch(answer_route):
    import sqlite3
    from alembic import command
    from storage.migrations import alembic_config

    r = answer_route
    path = r["engine"].url.database
    from pathlib import Path

    config = alembic_config(Path(path))
    command.downgrade(config, "20261118_0091")

    def snapshot():
        with sqlite3.connect(path) as conn:
            names = [
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if row[0] not in {"alembic_version", "message_answer_operations"}
            ]
            return {name: conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall() for name in names}

    before = snapshot()
    command.upgrade(config, "head")
    assert snapshot() == before
    command.downgrade(config, "20261118_0091")
    assert snapshot() == before
    command.upgrade(config, "head")
