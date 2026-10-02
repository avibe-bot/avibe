"""C-5 transcript store over ``messages`` and ``agent_events`` (agent-core-contracts/transcript.md).

Properties: a Session's context is exactly its rows with a ``context_seq``, plus
its fork ancestry up to the anchor, in one total order across both tables; every
payload kind reads back as written; ``context_seq`` is never allocated twice;
the output outbox replays pending responses in order.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

import pytest
from sqlalchemy import select

from core.agent_core.messages import (
    AssistantMessage,
    ImageBlock,
    Origin,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
    text,
)
from config.paths import get_sqlite_state_path
from storage import message_deliveries, messages_service
from storage.agent_transcript import RenderedDisplay, SQLiteTranscriptStore, TranscriptError, resolve_fork_anchor_seq
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_events, messages
from storage.models import message_deliveries as delivery_rows
from storage.settings_service import upsert_scope

NOW = "2026-10-02T00:00:00.000000Z"
ORIGIN = Origin(provider="anthropic", api="anthropic", model="claude-opus-5-5")


@pytest.fixture()
def engine():
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    yield engine
    engine.dispose()


def _scope(conn, native_id: str, platform: str = "slack") -> str:
    return upsert_scope(conn, platform=platform, scope_type="channel", native_id=native_id, now=NOW)


def _session(conn, session_id: str, scope_id: str, metadata: dict | None = None) -> None:
    conn.exec_driver_sql(
        "insert into agent_sessions (id, scope_id, agent_name, agent_backend, agent_variant, session_anchor, "
        "workdir, native_session_id, status, visibility, pinned, agent_status, metadata_json, created_at, "
        "updated_at, last_active_at) values (?, ?, 'avibe', 'avibe', 'avibe', ?, '/tmp', ?, 'active', "
        "'foreground', 0, 'idle', ?, ?, ?, ?)",
        (session_id, scope_id, session_id, session_id, json.dumps(metadata or {}), NOW, NOW, NOW),
    )


def _fork(conn, session_id: str, scope_id: str, source_id: str, anchor_id: str | None) -> None:
    """A fork as reservation records it, with the anchor resolved by the transcript rule."""
    metadata = {"created_via": "session_fork", "fork_source_session_id": source_id, "fork_source_message_id": anchor_id}
    if anchor_id is not None:
        metadata["fork_source_context_seq"] = resolve_fork_anchor_seq(conn, source_id, anchor_id)
    _session(conn, session_id, scope_id, metadata)


def _row(conn, session_id: str, scope_id: str, body: str, *, kind: str = "user", platform: str = "slack") -> str:
    author = "user" if kind == "user" else "agent"
    row = messages_service.append(
        conn,
        scope_id=scope_id,
        session_id=session_id,
        platform=platform,
        author=author,
        source=author,
        message_type=kind,
        text=body,
    )
    return row["id"]


def _accepted_turn(conn, session_id: str, scope_id: str, body: str, turn_id: str) -> str:
    """An input accepted through the real Delivery path; returns its message id."""
    delivery = message_deliveries.insert_delivery(
        conn,
        delivery_id=f"dlv_{turn_id}",
        session_id=session_id,
        priority="p3",
        state="reserved",
        snapshot=message_deliveries.message_snapshot(
            scope_id=scope_id,
            session_id=session_id,
            platform="slack",
            author="user",
            source="user",
            message_type="user",
            text=body,
        ),
        dispatch_text=body,
        now=NOW,
    )
    claimed = message_deliveries.claim_start_batch(
        conn, turn_id=turn_id, session_id=session_id, backend="avibe", deliveries=[delivery], dispatch_text=body
    )
    assert message_deliveries.bind_native_start(
        conn,
        turn_id,
        expected_version=int(claimed["turn"]["version"]),
        runtime_key="runtime",
        runtime_turn_id=turn_id,
        native_turn_id=turn_id,
    )
    assert message_deliveries.materialize_start_acceptance(conn, turn_id=turn_id, evidence={"kind": "test"})
    return conn.execute(
        select(delivery_rows.c.message_id).where(delivery_rows.c.id == f"dlv_{turn_id}")
    ).scalar_one()


def _accepted_steer(conn, session_id: str, scope_id: str, body: str, turn_id: str) -> str:
    message_id = _row(conn, session_id, scope_id, body, platform="avibe")
    conn.execute(
        delivery_rows.insert().values(
            id=f"dlv_{message_id}",
            session_id=session_id,
            message_id=message_id,
            priority="p1",
            state="accepted",
            snapshot_sha256="steer",
            dispatch_sha256="steer",
            turn_id=turn_id,
            turn_role="steer",
            turn_position=1,
            submitted_at=NOW,
            updated_at=NOW,
            materialized_at=NOW,
        )
    )
    return message_id


def _user(body: str) -> UserMessage:
    return UserMessage(content=(text(body),))


def _assistant(body: str, *, call_id: str | None = None) -> AssistantMessage:
    content: tuple = (text(body),)
    if call_id is not None:
        content = (
            ThinkingBlock(text="look first", signature="sig-1"),
            text(body),
            ToolCallBlock(id=call_id, name="bash", arguments={"command": "ls"}),
        )
    return AssistantMessage(
        content=content,
        origin=ORIGIN,
        stop_reason="tool_use" if call_id else "stop",
        usage=Usage(input_tokens=10, output_tokens=2),
    )


def _tool_result(call_id: str, body: str) -> ToolResultMessage:
    return ToolResultMessage(tool_call_id=call_id, tool_name="bash", content=(text(body),))


COMPACTION = {
    "version": 1,
    "summary": "## Objective\n修复路径测试",
    "first_kept_seq": 3,
    "summarized_from_seq": 1,
    "summarized_to_seq": 2,
    "previous_compaction_id": None,
    "reason": "manual",
    "focus": None,
    "tokens_before": 100,
    "tokens_after_estimate": 40,
    "summarizer": {"origin": {"provider": "anthropic", "api": "anthropic", "model": "claude-opus-5-5"},
                   "prompt_version": "ckpt-v1", "chunks": 1},
    "files_read": ["core/paths.py"],
    "files_modified": [],
    "current_request_message_id": None,
}


async def test_every_kind_round_trips_in_one_order_attributed_to_the_turn(engine) -> None:
    with engine.begin() as conn:
        home = _scope(conn, "C-home")
        elsewhere = _scope(conn, "C-elsewhere", platform="avibe")
        _session(conn, "ses_main", home)
        first = _accepted_turn(conn, "ses_main", home, "看一下目录", "turn_main")
        steer = _accepted_steer(conn, "ses_main", elsewhere, "also check tests", "turn_main")
    store = SQLiteTranscriptStore(engine)
    input_message = UserMessage(content=(text("[10:00] 看一下目录"), ImageBlock("image/png", "tok_1", name="s.png")))

    written = [
        await store.consume_input("ses_main", first, input_message),
        await store.append_response("ses_main", _assistant("Listing.", call_id="call_1"), final=False),
        await store.append_tool_result(
            "ses_main", _tool_result("call_1", "a.py\nb.py"), details={"exit_code": 0, "job_id": "job_1"}
        ),
        await store.consume_input("ses_main", steer, _user("also check tests")),
        await store.append_payload("ses_main", "agent_state", {"version": 1, "state": {"mode": "plan"}}),
        await store.append_payload(
            "ses_main",
            "context_edit",
            {"version": 1, "target_event_id": "evt_x", "replacement": {"text": "[cleared]"},
             "reason": "clear_old_tool_result"},
        ),
        await store.append_payload("ses_main", "compaction", COMPACTION),
        await store.append_response("ses_main", _assistant("两个文件。"), final=True),
    ]

    assert [entry.context_seq for entry in written] == list(range(1, 9))
    assert [entry.kind for entry in written] == [
        "input", "response", "tool_result", "input", "agent_state", "context_edit", "compaction", "response",
    ]
    assert list(await SQLiteTranscriptStore(engine).load("ses_main")) == written
    with engine.connect() as conn:
        rows = {
            row["id"]: row
            for row in conn.execute(select(messages).where(messages.c.session_id == "ses_main")).mappings()
        }
        events = conn.execute(
            select(agent_events).where(agent_events.c.session_id == "ses_main").order_by(agent_events.c.context_seq)
        ).mappings().all()
    assert rows[first]["content_text"] == "看一下目录"
    assert json.loads(rows[first]["content_json"])["text"] == "看一下目录"
    for entry, row_type, display in ((written[1], "assistant", "Listing."), (written[7], "result", "两个文件。")):
        row = rows[entry.row_id]
        assert (row["type"], row["content_text"], row["author"]) == (row_type, display, "agent")
        assert json.loads(row["metadata_json"])["delivery"] == {"state": "pending", "parts": []}
    # Every inserted row answers the Turn's initial input, not the steer's surface.
    inserted = [rows[written[1].row_id], rows[written[7].row_id], *events]
    assert {(row["platform"], row["scope_id"]) for row in inserted} == {("slack", home)}
    assert {row["turn_id"] for row in events} == {"turn_main"}
    assert [(row["event_type"], row["visibility"]) for row in events] == [
        ("tool_result", "context"),
        ("agent_state", "context"),
        ("context_edit", "context"),
        ("context_compaction", "context"),
    ]


async def test_rows_outside_the_context_never_load(engine) -> None:
    with engine.begin() as conn:
        home = _scope(conn, "C-home")
        _session(conn, "ses_main", home)
        consumed = _row(conn, "ses_main", home, "go")
        queued = _row(conn, "ses_main", home, "queued, never consumed")
        _row(conn, "ses_main", home, "status", kind="notify")
    store = SQLiteTranscriptStore(engine)
    entry = await store.consume_input("ses_main", consumed, _user("go"))
    with engine.begin() as conn:
        conn.execute(
            agent_events.insert().values(
                id="evt_trace", session_id="ses_main", platform="slack", event_type="tool_call",
                visibility="trace", content_json="{}", metadata_json="{}", created_at=NOW, updated_at=NOW,
            )
        )

    assert list(await store.load("ses_main")) == [entry]
    with engine.connect() as conn:
        assert conn.execute(select(messages.c.context_seq).where(messages.c.id == queued)).scalar() is None


async def test_an_input_enters_the_context_once(engine) -> None:
    with engine.begin() as conn:
        home = _scope(conn, "C-home")
        _session(conn, "ses_main", home)
        _session(conn, "ses_other", home)
        consumed = _row(conn, "ses_main", home, "go")
        reply = _row(conn, "ses_main", home, "done", kind="result")
        foreign = _row(conn, "ses_other", home, "elsewhere")
    store = SQLiteTranscriptStore(engine)
    entry = await store.consume_input("ses_main", consumed, _user("go"))

    assert await store.consume_input("ses_main", consumed, _user("go")) == entry
    for message_id, message in ((consumed, _user("edited")), (reply, _user("done")), (foreign, _user("x"))):
        with pytest.raises(TranscriptError):
            await store.consume_input("ses_main", message_id, message)
    assert list(await store.load("ses_main")) == [entry]


async def test_concurrent_writers_never_share_a_context_seq(engine) -> None:
    with engine.begin() as conn:
        home = _scope(conn, "C-home")
        _session(conn, "ses_main", home)
        consumed = _row(conn, "ses_main", home, "go")
    # Two stores on two engines stand in for two processes: only SQLite orders them.
    other_engine = create_sqlite_engine()
    stores = (SQLiteTranscriptStore(engine), SQLiteTranscriptStore(other_engine))
    try:
        await stores[0].consume_input("ses_main", consumed, _user("go"))
        await asyncio.gather(
            *(
                stores[index % 2].append_tool_result("ses_main", _tool_result(f"call_{index}", "ok"), details={})
                for index in range(40)
            ),
            *(stores[index % 2].append_response("ses_main", _assistant(f"r{index}"), final=False) for index in range(20)),
        )
    finally:
        other_engine.dispose()

    loaded = await stores[0].load("ses_main")
    assert [entry.context_seq for entry in loaded] == list(range(1, 62))


async def test_fork_chain_reads_the_ancestry_up_to_each_anchor(engine) -> None:
    with engine.begin() as conn:
        home = _scope(conn, "C-home")
        _session(conn, "ses_root", home)
        ask = _row(conn, "ses_root", home, "ask")
    store = SQLiteTranscriptStore(engine)
    root = [
        await store.consume_input("ses_root", ask, _user("ask")),
        await store.append_response("ses_root", _assistant("run", call_id="call_1"), final=False),
        await store.append_tool_result("ses_root", _tool_result("call_1", "ok"), details={}),
    ]
    with engine.begin() as conn:
        # A steer accepted before the anchor but consumed after the fork, and a
        # display-only anchor whose last preceding context row is a tool result.
        steer = _row(conn, "ses_root", home, "steer")
        anchor = _row(conn, "ses_root", home, "progress", kind="notify")
        _fork(conn, "ses_child", home, "ses_root", anchor)
        # Released forks and forks of other backends carry no anchor_seq.
        _session(conn, "ses_released", home, {"fork_source_session_id": "ses_root", "fork_source_message_id": anchor})
    await store.consume_input("ses_root", steer, _user("steer"))
    await store.append_payload("ses_root", "compaction", {**COMPACTION, "first_kept_seq": 4})

    with engine.begin() as conn:
        child_ask = _row(conn, "ses_child", home, "child ask")
        released_ask = _row(conn, "ses_released", home, "fresh")
    child = [
        await store.consume_input("ses_child", child_ask, _user("child ask")),
        await store.append_response("ses_child", _assistant("child done"), final=True),
    ]
    with engine.begin() as conn:
        _fork(conn, "ses_grandchild", home, "ses_child", child[1].row_id)
        grandchild_ask = _row(conn, "ses_grandchild", home, "grandchild ask")
        # A fork at an earlier message of a Session that has moved on.
        _fork(conn, "ses_early", home, "ses_root", ask)
        early_ask = _row(conn, "ses_early", home, "early ask")
    grandchild = await store.consume_input("ses_grandchild", grandchild_ask, _user("grandchild ask"))
    released = await store.consume_input("ses_released", released_ask, _user("fresh"))
    early = await store.consume_input("ses_early", early_ask, _user("early ask"))

    # The child continues after the anchor; the root's later steer and checkpoint
    # (4, 5) are not in its context.
    assert [entry.context_seq for entry in child] == [4, 5]
    assert list(await store.load("ses_child")) == root + child
    assert list(await store.load("ses_grandchild")) == [*root, *child, grandchild]
    assert grandchild.context_seq == 6
    assert [entry.kind for entry in await store.load("ses_root")][-2:] == ["input", "compaction"]
    assert list(await store.load("ses_released")) == [released]
    assert released.context_seq == 1
    assert list(await store.load("ses_early")) == [root[0], early]
    assert early.context_seq == 2


async def test_outbox_replays_pending_responses_until_every_part_is_confirmed(engine) -> None:
    with engine.begin() as conn:
        home = _scope(conn, "C-home")
        _session(conn, "ses_main", home)
        consumed = _row(conn, "ses_main", home, "go")
    store = SQLiteTranscriptStore(engine)
    await store.consume_input("ses_main", consumed, _user("go"))
    narration = await store.append_response("ses_main", _assistant("step", call_id="call_1"), final=False)
    await store.append_tool_result("ses_main", _tool_result("call_1", "ok"), details={})
    # A response with nothing to display has no part on any surface.
    silent = AssistantMessage(content=(ToolCallBlock(id="call_2", name="bash"),), origin=ORIGIN, stop_reason="tool_use")
    await store.append_response("ses_main", silent, final=False)
    await store.append_tool_result("ses_main", _tool_result("call_2", "ok"), details={})
    reply = await store.append_response("ses_main", _assistant("answer"), final=True)

    pending = await store.pending_deliveries("ses_main")
    assert [(item.row_id, item.context_seq, item.final, item.text, item.parts) for item in pending] == [
        (narration.row_id, 2, False, "step", ()),
        (reply.row_id, 6, True, "answer", ()),
    ]
    assert pending[1].message == reply.message

    assert await store.record_delivery_part("ses_main", narration.row_id, index=0, count=1) is True
    # A reply split in two stays pending until both parts are confirmed, and a
    # retried part keeps its first receipt.
    assert await store.record_delivery_part("ses_main", reply.row_id, index=1, count=2, native_message_id="b") is False
    assert await store.record_delivery_part("ses_main", reply.row_id, index=1, count=2, native_message_id="x") is False
    [still_pending] = await store.pending_deliveries("ses_main")
    assert still_pending.row_id == reply.row_id
    assert [part and part["native_message_id"] for part in still_pending.parts] == [None, "b"]
    with pytest.raises(TranscriptError):
        await store.record_delivery_part("ses_main", reply.row_id, index=0, count=3)
    assert await store.record_delivery_part("ses_main", reply.row_id, index=0, count=2, native_message_id="a") is True

    assert await store.pending_deliveries("ses_main") == []
    with engine.connect() as conn:
        delivery = json.loads(
            conn.execute(select(messages.c.metadata_json).where(messages.c.id == reply.row_id)).scalar_one()
        )["delivery"]
    assert delivery["state"] == "delivered"
    assert [part["native_message_id"] for part in delivery["parts"]] == ["a", "b"]
    with pytest.raises(TranscriptError):
        await store.record_delivery_part("ses_main", consumed, index=0, count=1)


async def test_a_cancelled_write_raises_only_after_its_commit_settled(engine) -> None:
    with engine.begin() as conn:
        home = _scope(conn, "C-home")
        _session(conn, "ses_main", home)
        consumed = _row(conn, "ses_main", home, "go")
    store = SQLiteTranscriptStore(engine)
    await store.consume_input("ses_main", consumed, _user("go"))
    # Another writer holds SQLite's lock, so the append waits inside its thread.
    holder = sqlite3.connect(get_sqlite_state_path(), isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.5, holder.execute, ("COMMIT",))
    append = asyncio.create_task(store.append_tool_result("ses_main", _tool_result("call_1", "ok"), details={}))
    await asyncio.sleep(0.1)
    release.start()
    append.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await append
        # Recovery after the cancellation already sees the commit, so it cannot append it again.
        assert [entry.kind for entry in await store.load("ses_main")] == ["input", "tool_result"]
    finally:
        release.join()
        holder.close()


@pytest.mark.parametrize(
    "value",
    [("a", "b"), {1: "x"}, float("nan"), float("inf"), b"raw"],
    ids=["tuple", "non-str key", "nan", "inf", "bytes"],
)
async def test_payloads_that_would_not_read_back_unchanged_are_refused(engine, value) -> None:
    with engine.begin() as conn:
        home = _scope(conn, "C-home")
        _session(conn, "ses_main", home)
        consumed = _row(conn, "ses_main", home, "go")
    store = SQLiteTranscriptStore(engine)
    entry = await store.consume_input("ses_main", consumed, _user("go"))

    with pytest.raises(ValueError):
        await store.append_payload("ses_main", "agent_state", {"version": 1, "state": {"value": value}})
    with pytest.raises(ValueError):
        await store.append_tool_result("ses_main", _tool_result("call_1", "ok"), details={"value": value})
    assert list(await store.load("ses_main")) == [entry]


async def test_the_display_copy_commits_with_its_row_and_never_reaches_the_context(engine) -> None:
    with engine.begin() as conn:
        home = _scope(conn, "C-home")
        _session(conn, "ses_main", home)
        consumed = _row(conn, "ses_main", home, "go")
    seen: list[tuple] = []

    def render(message, *, final, conn, platform, scope_id, session_id):
        # A rewrite inside the transaction sees the row's own attribution and connection.
        seen.append((final, platform, scope_id, session_id, conn.in_transaction()))
        body = "\n".join(block.text for block in message.content if isinstance(block, TextBlock) and block.text)
        if not final:
            return body.upper()
        return RenderedDisplay(text=f"{body} (shown)", content={"kind": "result", "quick_replies": ["继续"]})

    store = SQLiteTranscriptStore(engine, render=render)
    await store.consume_input("ses_main", consumed, _user("go"))
    narration = await store.append_response("ses_main", _assistant("step", call_id="call_1"), final=False)
    await store.append_tool_result("ses_main", _tool_result("call_1", "ok"), details={})
    reply = await store.append_response("ses_main", _assistant("答案"), final=True)

    assert seen == [(False, "slack", home, "ses_main", True), (True, "slack", home, "ses_main", True)]
    with engine.connect() as conn:
        rows = {
            row["id"]: row
            for row in conn.execute(select(messages).where(messages.c.id.in_([narration.row_id, reply.row_id])))
            .mappings()
        }
    assert rows[narration.row_id]["content_text"] == "STEP"
    assert rows[reply.row_id]["content_text"] == "答案 (shown)"
    reply_content = json.loads(rows[reply.row_id]["content_json"])
    assert (reply_content["kind"], reply_content["quick_replies"]) == ("result", ["继续"])
    # The model copy is the response verbatim; the display copy is not context.
    assert [entry.message for entry in await store.load("ses_main") if entry.kind == "response"] == [
        narration.message,
        reply.message,
    ]

    def overwrite_model(message, **_attribution):
        return RenderedDisplay(text="x", content={"model": {"version": 1}})

    with pytest.raises(TranscriptError):
        await SQLiteTranscriptStore(engine, render=overwrite_model).append_response(
            "ses_main", _assistant("again"), final=True
        )
    assert [entry.row_id for entry in await store.load("ses_main")][-1] == reply.row_id


async def test_a_delivery_plan_commits_with_its_row_and_bounds_its_receipts(engine) -> None:
    with engine.begin() as conn:
        home = _scope(conn, "C-home")
        _session(conn, "ses_main", home)
        consumed = _row(conn, "ses_main", home, "go")
    plan = {"version": 1, "final": True, "parts": [{"kind": "text", "text": "a"}, {"kind": "file", "path": "/r"}]}

    def render(message, *, final, **_attribution):
        parts = plan["parts"] if final else []
        return RenderedDisplay(text="shown", delivery={**plan, "final": final, "parts": parts})

    store = SQLiteTranscriptStore(engine, render=render)
    await store.consume_input("ses_main", consumed, _user("go"))
    # A response whose plan has nothing to send is delivered as it commits.
    narration = await store.append_response("ses_main", _assistant("step", call_id="call_1"), final=False)
    await store.append_tool_result("ses_main", _tool_result("call_1", "ok"), details={})
    reply = await store.append_response("ses_main", _assistant("answer"), final=True)
    assert await store.delivery("ses_main", narration.row_id) is None

    pending = await store.delivery("ses_main", reply.row_id)
    assert pending.plan == plan and pending.footer is None
    await store.settle_delivery("ses_main", reply.row_id, footer="✅ done", display={"result_footer": "✅ done"})
    with pytest.raises(TranscriptError):
        await store.record_delivery_part("ses_main", reply.row_id, index=0, count=3)
    assert await store.record_delivery_part("ses_main", reply.row_id, index=0, count=2, native_message_id="m1") is False
    # Once a part is out, settlement no longer changes what the remaining parts show.
    await store.settle_delivery("ses_main", reply.row_id, footer="❌ failed", display={"result_footer": "❌ failed"})
    pending = await store.delivery("ses_main", reply.row_id)
    assert (pending.footer, [part is not None for part in pending.parts]) == ("✅ done", [True, False])
    assert await store.record_delivery_part("ses_main", reply.row_id, index=1, count=2, skipped="file_missing") is True
    assert await store.delivery("ses_main", reply.row_id) is None
    settled = await store.delivery("ses_main", reply.row_id, include_delivered=True)
    assert settled.parts[1]["skipped"] == "file_missing" and settled.plan == plan
    with engine.connect() as conn:
        content = json.loads(conn.execute(select(messages.c.content_json).where(messages.c.id == reply.row_id)).scalar())
    assert content["result_footer"] == "✅ done" and "model" in content
    with pytest.raises(TranscriptError):
        await store.settle_delivery("ses_main", reply.row_id, footer=None, display={"model": {}})
