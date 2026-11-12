"""Activity reads batch bounded canonical seeks and cache only within one read."""
from __future__ import annotations

import json

from sqlalchemy import event, select

from core.agent_core.messages import ToolCallBlock, ToolResultMessage, text
from storage import agent_activity_service, tool_activity_service
from storage.models import agent_events, messages
from tests.agent_core.fakes import assistant
from tests.test_tool_activity_projection import _mark, _store
from tests.test_vibey_agent import SESSION, engine, session  # noqa: F401 -- isolated store fixtures


async def test_many_responses_batch_seeks_without_changing_per_response_limits(engine, session, monkeypatch):
    store = await _store(engine)
    response = await store.append_response(
        SESSION, assistant("", calls=(ToolCallBlock("reused", "read", {}),)), final=False, metadata=_mark(),
    )
    await store.append_tool_result(SESSION, ToolResultMessage("reused", "read", (text("first result"),)), details={})
    with engine.begin() as conn:
        source = dict(conn.execute(select(messages).where(messages.c.id == response.row_id)).mappings().one())
        result = dict(conn.execute(select(agent_events).where(agent_events.c.event_type == "tool_result")).mappings().one())
        conn.execute(messages.delete().where(messages.c.id == response.row_id))
        conn.execute(agent_events.delete().where(agent_events.c.id == result["id"]))
        # Exercise three batches with tiny rows, not a load test. An extra result
        # in every interval must not defeat each response's own call-count limit.
        size = tool_activity_service._READ_BATCH_SIZE * 2 + 1
        ids = [f"response-{i:04d}" for i in range(size)]
        conn.execute(messages.insert(), [
            {**source, "id": row_id, "context_seq": response.context_seq + 3 * i}
            for i, row_id in enumerate(ids)
        ])
        conn.execute(agent_events.insert(), [
            {**result, "id": f"result-{i:04d}-{offset}", "context_seq": response.context_seq + 3 * i + offset}
            for i in range(size) for offset in (1, 2)
        ])

    statements = []
    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))
    import core.tool_activity as projection
    constructed = []
    original = projection.formatter_for
    def formatter(platform):
        constructed.append(platform)
        return original(platform)
    monkeypatch.setattr(projection, "formatter_for", formatter)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with engine.connect() as conn:
            steps = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=ids)
            assert len(steps) == size
            assert {step["response_id"] for step in steps} == set(ids)
            assert all(step["status"] == "completed" for step in steps)
            # Messages + Turns + two bounded statements per batch, not 2*N.
            assert len(statements) == 8
            assert constructed == ["avibe"]
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    with engine.connect() as conn:
        plans = [plan[3] for sql, parameters in statements
                 for plan in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters)]
        assert not any("SCAN messages" in plan or "SCAN agent_events" in plan
                       or "SCAN next_response" in plan or "TEMP B-TREE" in plan for plan in plans), plans
        assert any("SEARCH next_response" in plan and "context_seq" in plan for plan in plans), plans
        assert any("SEARCH agent_events" in plan and "context_seq" in plan for plan in plans), plans
        retained = [dict(row) for row in conn.execute(select(messages).where(messages.c.id.in_(ids))).mappings()]
        result_map = tool_activity_service._response_results(conn, retained)
        assert all([value["id"] for value in result_map[row_id]] == [f"result-{i:04d}-1"]
                   for i, row_id in enumerate(ids))
        # A second read gets its own formatter, not a process/connection cache.
        tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=ids)
        assert constructed == ["avibe", "avibe"]
        summary = agent_activity_service.list_turn_groups(conn, session_id=SESSION)
        assert sum(group["steps"] for group in summary["groups"]) == size
        detail = [agent_activity_service.get_turn_group(conn, session_id=SESSION, group_id=group["id"])
                  for group in summary["groups"]]
        assert sum(len(group["rows"]) for group in detail) == size


async def test_batch_boundary_outside_retained_ids_and_same_connection_refresh(engine, session):
    store = await _store(engine)
    first = await store.append_response(
        SESSION, assistant("", calls=(ToolCallBlock("same", "read", {}),)), final=False, metadata=_mark(),
    )
    barrier = await store.append_response(
        SESSION, assistant("", calls=(ToolCallBlock("same", "read", {}),)), final=False,
    )
    await store.append_tool_result(SESSION, ToolResultMessage("same", "read", (text("barrier result"),)), details={})
    last = await store.append_response(
        SESSION, assistant("", calls=(ToolCallBlock("same", "read", {}),)), final=False, metadata=_mark(),
    )
    with engine.begin() as conn:
        conn.execute(messages.update().where(messages.c.id == barrier.row_id).values(content_json='{"model":[]}'))
        retained = (first.row_id, last.row_id)
        before = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=retained)
        assert {step["status"] for step in before} == {"unknown"}
        result = dict(conn.execute(select(agent_events).where(agent_events.c.event_type == "tool_result")).mappings().one())
        conn.execute(agent_events.insert().values(
            **{**result, "id": "last-result", "context_seq": last.context_seq + 1},
        ))
        after = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=retained)
        assert {step["response_id"]: step["status"] for step in after} == {
            first.row_id: "unknown", last.row_id: "completed",
        }
        # A result correction is visible without reopening the connection either.
        payload = json.loads(result["content_json"])
        payload["message"]["is_error"] = True
        conn.execute(agent_events.update().where(agent_events.c.id == "last-result").values(content_json=json.dumps(payload)))
        corrected = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=retained)
        assert {step["response_id"]: step["status"] for step in corrected} == {
            first.row_id: "unknown", last.row_id: "failed",
        }
        # A deletion between the window read and boundary lookup is not evidence
        # of an open interval. In particular it cannot borrow the barrier's result.
        snapshots = [dict(row) for row in conn.execute(select(messages).where(messages.c.id.in_(retained))).mappings()]
        conn.execute(messages.delete().where(messages.c.id == first.row_id))
        results = tool_activity_service._response_results(conn, snapshots)
        assert results[first.row_id] == []
        assert [row["id"] for row in results[last.row_id]] == ["last-result"]
        conn.execute(messages.delete().where(messages.c.id == last.row_id))
        assert tool_activity_service._response_results(conn, snapshots) == {row_id: [] for row_id in retained}
