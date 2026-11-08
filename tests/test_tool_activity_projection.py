"""C-alt reader-first contracts at the projection and real adapter boundaries.

Legacy Activity tests only seed traces. These cover omission of that redundant
copy, exact call-instance joins, durable/live identity and evidence-backed state.
The existing hosted adapter matrix separately pins media/error/IM presentation.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import event, select

from core.agent_core.ai.provider import Done
from core.agent_core.messages import ToolCallBlock, ToolResultMessage, UserMessage, text
from core.tool_activity import project_response
from storage import agent_activity_service, agent_events_service, messages_service, tool_activity_service
from storage.agent_transcript import SQLiteTranscriptStore
from storage.models import agent_events, agent_sessions, messages
from storage.read_only_query import ReadOnlyQueryError, run_read_only_query
from tests.agent_core.fakes import FakeTool, assistant
from tests.test_vibey_agent import (  # noqa: F401 -- shared isolated real-store/Delivery harness
    SESSION, _Harness, _insert_session, engine, published, session,
)


def _mark(**fields):
    return {"tool_projection": {"version": 1, "backend": "vibey", "platform": "avibe", "cwd": "/work", "language": "en", **fields}}


async def _store(engine):
    store = SQLiteTranscriptStore(engine)
    with engine.begin() as conn:
        scope = conn.execute(select(agent_sessions.c.scope_id).where(agent_sessions.c.id == SESSION)).scalar()
        row = messages_service.append(conn, session_id=SESSION, scope_id=scope, platform="avibe", author="user", message_type="user", text="go")
    await store.consume_input(SESSION, row["id"], UserMessage((text("go"),)))
    return store


def _rows(conn):
    return [row for group in agent_activity_service.list_turn_groups(conn, session_id=SESSION)["groups"]
            for row in agent_activity_service.get_turn_group(conn, session_id=SESSION, group_id=group["id"])["rows"]]


def _trace(conn, response, call_id, *, marked=True, text="legacy line"):
    return agent_events_service.append(
        conn, session_id=SESSION, scope_id=None, platform="avibe", event_type="tool_call", visibility="trace", text=text,
        metadata={"tool_call_source": {"version": 1, "response_id": response.row_id, "call_id": call_id}} if marked else {},
    )


async def test_tool_only_response_owns_order_and_identity_without_its_compatibility_traces(engine, session):
    """An empty display body still owns two steps, even when its Session changes backend.

    Regression: today's reader ignores the tool-only message, and physical traces
    would supply different ids/order or double the count during reader adoption.
    """
    store = await _store(engine)
    calls = (ToolCallBlock("z-last-lexically", "read", {"path": "/work/a.py"}),
             ToolCallBlock("a-first-lexically", "read", {"path": "/work/a.py"}))
    response = await store.append_response(SESSION, assistant("", calls=calls), final=False, metadata=_mark())
    with engine.begin() as conn:
        legacy = _trace(conn, response, "legacy", marked=False)
        for call in calls:
            _trace(conn, response, call.id)
        conn.execute(agent_sessions.update().where(agent_sessions.c.id == SESSION).values(agent_backend="codex"))
    with engine.connect() as conn:
        rows = _rows(conn)
        assert [row["id"] for row in rows] == [f"tool:{response.row_id}:z-last-lexically", f"tool:{response.row_id}:a-first-lexically", legacy["id"]]
        assert [row["order_index"] for row in rows[:2]] == [0, 1]
        assert rows[0]["text"] == '🔧 `read` `{"path":"a.py"}`'
        assert rows[0]["status"] == "unknown" and rows[0]["started_at"] is None
    # Simulate the later writer's physical shape without changing this PR's writer.
    with engine.begin() as conn:
        conn.execute(agent_events.delete().where(agent_events.c.id != legacy["id"]))
    with engine.connect() as conn:
        assert _rows(conn) == rows
        assert conn.execute(select(messages.c.content_text).where(messages.c.id == response.row_id)).scalar() == ""


@pytest.mark.parametrize("version, future_shape", [(None, False), (2, False), (True, False), (2, True)])
async def test_unmarked_and_unknown_capabilities_keep_real_traces(engine, session, version, future_shape):
    store = await _store(engine)
    metadata = {} if version is None else _mark(version=version)
    if future_shape:
        metadata["turn_id"] = "future-turn"
    response = await store.append_response(SESSION, assistant("", calls=(ToolCallBlock("call", "read", {}),)), final=False, metadata=metadata)
    with engine.begin() as conn:
        if future_shape:
            conn.execute(messages.update().where(messages.c.id == response.row_id).values(content_json='{"model":{"message":[]}}'))
        trace = _trace(conn, response, "call")
    with engine.connect() as conn:
        [row] = _rows(conn)
        assert row["id"] == trace["id"]
        assert row["order_micros"] == int(trace["id"][4:19], 16)


async def test_reused_call_ids_and_fork_results_never_settle_another_response(engine, session):
    """A later retry/fork success cannot claim that an earlier unstarted call ran."""
    store = await _store(engine)
    call = ToolCallBlock("same/id", "read", {"path": "a"})
    first = await store.append_response(SESSION, assistant("", calls=(call,)), final=False, metadata=_mark())
    second = await store.append_response(SESSION, assistant("", calls=(call,)), final=False, metadata=_mark())
    await store.append_tool_result(SESSION, ToolResultMessage(call.id, call.name, (text("second result"),)), details={})
    with engine.begin() as conn:
        scope = conn.execute(select(agent_sessions.c.scope_id).where(agent_sessions.c.id == SESSION)).scalar()
        _insert_session(conn, "child", scope, {"fork_source_session_id": SESSION, "fork_source_context_seq": first.context_seq})
    await store.append_tool_result("child", ToolResultMessage(call.id, call.name, (text("child result"),)), details={"recovered": True})
    with engine.connect() as conn:
        rows = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=(first.row_id, second.row_id))
        by_id = {row["response_id"]: row for row in rows}
        assert by_id[first.row_id]["status"] == "unknown"
        assert by_id[first.row_id]["started_at"] is None
        assert by_id[second.row_id]["status"] == "completed"
        assert by_id[second.row_id]["id"] == f"tool:{second.row_id}:same%2Fid"
        assert tool_activity_service.response_steps(conn, session_id="child", response_ids=(first.row_id,)) == []
        query = run_read_only_query("select response_id, status from tool_calls where session_id = 'ses_vibey' order by context_seq", db_path=Path(engine.url.database), page_request=None)
        assert query.rows == [{"response_id": first.row_id, "status": "unknown"}, {"response_id": second.row_id, "status": "completed"}]


async def test_an_opaque_context_response_still_bounds_the_previous_call_instance(engine, session):
    """A future/legacy response envelope cannot let earlier calls take its result.

    Ordinary non-context IM rows have no context_seq and are not barriers. A
    sequenced response remains a boundary even when this reader cannot decode it.
    """
    store = await _store(engine)
    call = ToolCallBlock("same", "read", {})
    first = await store.append_response(SESSION, assistant("", calls=(call,)), final=False, metadata=_mark())
    opaque = await store.append_response(SESSION, assistant("", calls=(call,)), final=False)
    await store.append_tool_result(SESSION, ToolResultMessage("same", "read", (text("later result"),)), details={})
    with engine.begin() as conn:
        conn.execute(messages.update().where(messages.c.id == opaque.row_id).values(content_json='{"kind":"assistant"}'))
        legacy = _trace(conn, opaque, "same", marked=False)
    with engine.connect() as conn:
        [step] = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=(first.row_id, opaque.row_id))
        assert step["status"] == "unknown"
    result = run_read_only_query("select id, status from tool_calls order by source", db_path=Path(engine.url.database), page_request=None)
    assert result.rows == [{"id": f"tool:{first.row_id}:same", "status": "unknown"}, {"id": legacy["id"], "status": "unknown"}]


@pytest.mark.parametrize(("started", "terminal", "error", "recovered", "expected"), [
    (False, False, None, False, "pending"), (True, False, None, False, "running"),
    (False, True, None, False, "interrupted"), (True, True, None, False, "interrupted"),
    (False, True, False, False, "completed"), (True, True, True, False, "failed"),
    (False, True, True, True, "interrupted"),
])
async def test_status_is_evidence_not_a_response_timestamp(engine, session, started, terminal, error, recovered, expected):
    store = await _store(engine)
    response = await store.append_response(SESSION, assistant("", calls=(ToolCallBlock("call", "read", {}),)), final=False, metadata=_mark())
    with engine.begin() as conn:
        if started:
            tool_activity_service.record_display_facts(conn, session_id=SESSION, response_id=response.row_id,
                                                      call_id="call", started_at="2026-11-08T01:00:01.123456Z")
    if error is not None:
        await store.append_tool_result(SESSION, ToolResultMessage("call", "read", (text("result"),), is_error=error), details={"recovered": recovered})
    with engine.connect() as conn:
        row = dict(conn.execute(select(messages).where(messages.c.id == response.row_id)).mappings().one())
        [step] = project_response(row, tool_activity_service.response_results(conn, row), {"state": "terminal" if terminal else "active"})
        assert step["status"] == expected
        assert step["started_at"] == ("2026-11-08T01:00:01.123456Z" if started else None)
        assert step["duration_ms"] is None


async def test_durable_turn_cancellation_settles_requested_calls_without_start_evidence(engine, session, tmp_path):
    """Storage must join the owning Turn, not leave a canceled request running/pending."""
    from storage import message_deliveries
    from tests.test_vibey_agent import _turn

    harness = _Harness(engine, tmp_path, "avibe", [])
    request = harness.request("work")
    harness.controller._native_start(request.context)
    turn_id = _turn(request.context)
    store = SQLiteTranscriptStore(engine)
    await store.consume_input(SESSION, request.context.platform_specific["delivery_id"], UserMessage((text("work"),)))
    response = await store.append_response(SESSION, assistant("", calls=(ToolCallBlock("call", "echo", {}),)), final=False, metadata=_mark(), turn_id=turn_id)
    with engine.connect() as conn:
        [pending] = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=(response.row_id,))
        assert pending["status"] == "pending" and pending["started_at"] is None
    with engine.begin() as conn:
        message_deliveries.terminalize_turn(conn, turn_id, outcome="canceled", settled_by="test_stop", evidence_kind="test")
    with engine.connect() as conn:
        [stopped] = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=(response.row_id,))
        assert stopped["status"] == "interrupted" and stopped["started_at"] is None
        assert stopped["id"] == pending["id"]
    query = run_read_only_query("select status, started_at from tool_calls where session_id = 'ses_vibey'", db_path=Path(engine.url.database), page_request=None)
    assert query.rows == [{"status": "interrupted", "started_at": None}]


async def test_sql_projection_deduplicates_sources_and_keeps_query_acl(engine, session):
    store = await _store(engine)
    response = await store.append_response(SESSION, assistant("", calls=(ToolCallBlock("call", "read", {}),)), final=False, metadata=_mark())
    with engine.begin() as conn:
        _trace(conn, response, "call")
        legacy = _trace(conn, response, "legacy", marked=False, text="physical")
    db = Path(engine.url.database)
    result = run_read_only_query("select id, source, source_id, name, arguments_json, status from tool_calls order by source", db_path=db, page_request=None)
    assert result.rows == [
        {"id": f"tool:{response.row_id}:call", "source": "canonical", "source_id": response.row_id, "name": "read", "arguments_json": "{}", "status": "unknown"},
        {"id": legacy["id"], "source": "trace", "source_id": legacy["id"], "name": None, "arguments_json": None, "status": "unknown"},
    ]
    from storage.resource_access_service import ResourceUserContext
    guest = ResourceUserContext(is_remote=True, instance_role="viewer", subject="guest")
    with pytest.raises(ReadOnlyQueryError, match="prohibited|authorized"):
        run_read_only_query("select * from tool_calls", db_path=db, page_request=None, user_context=guest)
    with pytest.raises(ReadOnlyQueryError):
        run_read_only_query("delete from tool_calls", db_path=db, page_request=None)
    assert run_read_only_query("select count(*) as n from agent_events where event_type = 'tool_call'", db_path=db, page_request=None).rows == [{"n": 2}]


async def test_retained_window_and_query_plans_bound_canonical_reads(engine, session, monkeypatch):
    """Summary/detail share their window; per-step SQL never scans growing history."""
    store = await _store(engine)
    responses = [await store.append_response(SESSION, assistant("", calls=(ToolCallBlock("same", "read", {}),)), final=False, metadata=_mark()) for _ in range(3)]
    await store.append_tool_result(SESSION, ToolResultMessage("same", "read", (text("done"),)), details={})
    with engine.begin() as conn:
        # A late compatibility write must not resurrect a response outside the
        # retained window under a different (physical event) identity.
        _trace(conn, responses[0], "same")
    monkeypatch.setattr(agent_activity_service, "MESSAGE_SCAN_LIMIT", 2)
    statements = []
    def captured(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))
    event.listen(engine, "before_cursor_execute", captured)
    try:
        with engine.connect() as conn:
            rows = _rows(conn)
            assert [row["id"] for row in rows] == [f"tool:{response.row_id}:same" for response in responses[-2:]]
    finally:
        event.remove(engine, "before_cursor_execute", captured)
    with engine.connect() as conn:
        plans = [row[3] for statement, params in statements if "context_seq >" in statement
                 for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, params)]
    assert plans and not any("SCAN messages" in plan or "SCAN agent_events" in plan or "TEMP B-TREE" in plan for plan in plans), plans
    assert any("SEARCH agent_events" in plan and "context_seq" in plan for plan in plans), plans


@pytest.mark.parametrize("platform,background,enabled", [("avibe", False, True), ("avibe", True, True), ("avibe", False, False), ("telegram", False, True)])
async def test_real_adapter_links_compatibility_writes_and_publishes_canonical_steps(engine, session, tmp_path, published, monkeypatch, platform, background, enabled):
    """The producer marks its own rows, preserves IM/old-reader writes and Web gates.

    The reader tests cannot catch dropped output metadata at the dispatcher or a
    live path still publishing the physical trace id.
    """
    from modules.im.formatters.avibe_formatter import AvibeFormatter
    from modules.im.formatters.telegram_formatter import TelegramFormatter
    call = ToolCallBlock("call", "echo", {"path": "src"})
    harness = _Harness(engine, tmp_path, platform, [[Done(assistant("", calls=(call,)))], [Done(assistant("done"))]], tools=[FakeTool("echo")])
    harness.controller.im_client.formatter = AvibeFormatter() if platform == "avibe" else TelegramFormatter()
    monkeypatch.setattr("core.message_mirror._activity_streaming_enabled", lambda: enabled)
    request = harness.request("go")
    request.context.platform_specific["suppress_delivery"] = background
    await harness.agent.handle_message(request)
    with engine.connect() as conn:
        [trace] = conn.execute(select(agent_events).where(agent_events.c.session_id == SESSION, agent_events.c.event_type == "tool_call")).mappings().all()
        [step] = [row for row in _rows(conn) if row["kind"] == "tool_call"]
        source = json.loads(trace["metadata_json"])["tool_call_source"]
        response = conn.execute(select(messages).where(messages.c.id == source["response_id"])).mappings().one()
        cap = json.loads(response["metadata_json"])["tool_projection"]
        assert cap["version"] == 1 and cap["backend"] == "vibey"
        assert cap["calls"]["call"]["started_at"]
        assert not any(key in cap["calls"]["call"] for key in ("arguments", "text", "result"))
        assert step["id"] == f"tool:{response['id']}:call"
        assert step["text"] == trace["content_text"]
        assert step["status"] == "completed" and step["started_at"] and step["ended_at"]
        assert step["duration_ms"] >= 0
    live = [value for topic, value in published if topic == "message.new" and value.get("type") == "tool_call"]
    if platform == "avibe" and not background and enabled:
        assert live and {value["id"] for value in live} == {step["id"]}
        assert live[-1]["content"]["activity"] == {key: step[key] for key in ("order_micros", "order_index", "status", "started_at", "ended_at", "duration_ms")}
    else:
        assert not live


async def test_queued_tool_events_keep_their_response_when_the_producer_reuses_ids(engine, session, tmp_path, published, monkeypatch):
    """The producer can commit a retry while the event consumer is still showing its predecessor.

    Binding call ids in the store callback attributes the first execution to the
    second response. Delaying display, not execution, makes that race deterministic.
    """
    from modules.im.formatters.avibe_formatter import AvibeFormatter

    first = ToolCallBlock("reused", "echo", {"value": "first"})
    second = ToolCallBlock("reused", "echo", {"value": "second"})
    harness = _Harness(engine, tmp_path, "avibe", [
        [Done(assistant("", calls=(first,)))], [Done(assistant("", calls=(second,)))], [Done(assistant("done"))],
    ], tools=[FakeTool("echo")])
    harness.controller.im_client.formatter = AvibeFormatter()
    monkeypatch.setattr("core.message_mirror._activity_streaming_enabled", lambda: True)
    committed = asyncio.Event()
    response_ids = []
    observe = harness.agent.store._on_response
    def on_response(session_id, row_id, message, replied, images):
        observe(session_id, row_id, message, replied, images)
        if message.tool_calls:
            response_ids.append(row_id)
            if len(response_ids) == 2:
                committed.set()
    monkeypatch.setattr(harness.agent.store, "_on_response", on_response)
    emit = harness.agent._emit_native_hosted
    delayed = False
    async def delay_first(run, row_id):
        nonlocal delayed
        if not delayed:
            delayed = True
            await asyncio.wait_for(committed.wait(), timeout=5)
        await emit(run, row_id)
    monkeypatch.setattr(harness.agent, "_emit_native_hosted", delay_first)
    await harness.agent.handle_message(harness.request("twice"))
    assert len(response_ids) == 2
    with engine.connect() as conn:
        traces = conn.execute(select(agent_events).where(agent_events.c.event_type == "tool_call").order_by(agent_events.c.id)).mappings().all()
        assert [row["content_text"] for row in traces] == [
            '🔧 `echo` `{"value":"first"}`', '🔧 `echo` `{"value":"second"}`',
        ]
        assert [json.loads(row["metadata_json"])["tool_call_source"]["response_id"] for row in traces] == response_ids
        steps = [row for row in _rows(conn) if row["kind"] == "tool_call"]
        assert [row["id"] for row in steps] == [f"tool:{row_id}:reused" for row_id in response_ids]
        assert all(row["started_at"] and row["status"] == "completed" for row in steps)
    live = [value for topic, value in published if topic == "message.new" and value.get("type") == "tool_call"]
    assert {value["id"] for value in live} == {row["id"] for row in steps}


async def test_projection_updates_do_not_publish_from_a_retired_runtime(engine, session, tmp_path, published, monkeypatch):
    """The direct pending/settled path must not bypass the dispatcher's stale-turn gate."""
    store = await _store(engine)
    response = await store.append_response(SESSION, assistant("", calls=(ToolCallBlock("call", "echo", {}),)), final=False, metadata=_mark())
    harness = _Harness(engine, tmp_path, "avibe", [])
    request = harness.request("new turn")
    harness.controller.agent_service.emit_matches_runtime_turn = lambda _context: False
    monkeypatch.setattr("core.message_mirror._activity_streaming_enabled", lambda: True)
    await harness.controller.message_dispatcher.publish_tool_projection(
        request.context, response.row_id, call_id="call", facts={"started_at": "2026-11-08T01:00:00Z"},
    )
    assert not [payload for topic, payload in published if topic == "message.new"]
    with engine.connect() as conn:
        [step] = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=(response.row_id,))
        assert step["started_at"] == "2026-11-08T01:00:00Z"


@pytest.mark.parametrize("mode", ["proxy", "direct"])
async def test_hosted_images_use_the_exact_canonical_registration_without_a_trace(engine, session, tmp_path, mode):
    """Trace suppression must not be the operation that used to mint a usable URL.

    A later registration at the same path (or in another Session) cannot replace
    the canonical capability. Reads never register media or revive revoked URLs.
    """
    from core.agent_core.messages import HostedImage, HostedRecord, HostedResult, ProxyExecution
    from storage import media_service
    from storage.models import media_objects
    from tests.agent_core.fakes import ORIGIN
    from tests.test_vibey_agent import _png

    store = await _store(engine)
    image_path = tmp_path / "image.png"
    image_path.write_bytes(_png(b"\x00\x01\x02"))
    link = tmp_path / "image-link.png"
    link.symlink_to(image_path)
    image = HostedImage(path=str(link), mime="image/png")
    record = HostedRecord(status="completed", images=(image,))
    response = await store.append_response(SESSION, assistant("", calls=(ToolCallBlock("image", "image_generate", {"prompt": "a square"}),)), final=False, metadata=_mark())
    await store.append_tool_result(SESSION, ToolResultMessage("image", "image_generate", (text("saved"),), hosted=HostedResult(
        record=record, execution=ProxyExecution(executor_origin=ORIGIN, executor_source_id="src_image0001", executor_billing="metered", mode=mode),
    )), details={})
    with engine.begin() as conn:
        [registered] = conn.execute(select(media_objects)).mappings().all()
        assert registered["source"] == "agent_context" and registered["local_path"] == str(image_path)
        assert registered["session_id"] == SESSION
        image_path.write_bytes(_png(b"\x03\x04\x05"))
        replacement = media_service.register(conn, session_id=SESSION, scope_id=registered["scope_id"], kind="image", source="agent_reply", local_path=str(image_path))
        assert replacement != registered["token"]
    with engine.connect() as conn:
        before = conn.connection.driver_connection.total_changes
        [step] = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=(response.row_id,))
        [shown] = step["content"]["hosted"]["images"]
        assert shown["media_url"].split("?")[0] == f"/api/media/{registered['token']}"
        assert conn.connection.driver_connection.total_changes == before
        assert conn.execute(select(agent_events.c.id).where(agent_events.c.event_type == "tool_call")).first() is None
    with engine.begin() as conn:
        conn.execute(media_objects.update().where(media_objects.c.token == registered["token"]).values(revoked_at="2026-11-08T00:00:00Z"))
    with engine.connect() as conn:
        [step] = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=(response.row_id,))
        assert "media_url" not in step["content"]["hosted"]["images"][0]
