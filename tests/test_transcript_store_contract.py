"""The ``TranscriptStore`` contract, run against every store (C-5 ``transcript.md`` §2, C-9 ``context.md`` §9).

Each contract test runs on the in-memory store the engine tests use, on the SQLite store, and on the adapter's
store over it: a protocol method the real store lacks, or a behavior the fake has and the contract does not state,
fails here, so what a test proves on the fake holds for the real store. The second half drives C-9 through the
loop on the fake and on SQLite, across a restart: the request facts, the commit times, the one-transaction commits,
and the attempt audit the loop relies on.
"""

from __future__ import annotations

import inspect
import json
import time

import pytest
from sqlalchemy import select

from core.agent_core.agent.hooks import Snapshot
from core.agent_core.agent.loop import Agent
from core.agent_core.agent.models import RetryPolicy
from core.agent_core.ai.provider import Done, ProviderError
from core.agent_core.harness.context import ContextConfig, request_tokens
from core.agent_core.harness.store import TranscriptStore
from core.agent_core.messages import AssistantMessage, ToolCallBlock, ToolResultMessage, Usage, text, usage_to_dict
from modules.agents.avibe.store import AdapterTranscriptStore
from storage import agent_events_service, messages_service
from storage.agent_transcript import SQLiteTranscriptStore, resolve_fork_anchor_seq
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_events
from storage.settings_service import upsert_scope
from tests.agent_core.agent.test_compaction import (
    CHECKPOINT,
    LARGE,
    THRESHOLD,
    Model,
    billed,
    english,
    growing,
    make_agent,
    reader,
    run,
    tokens,
)
from tests.agent_core.fakes import (
    FakeJobHost,
    FakeModelRouter,
    InMemoryTranscriptStore,
    ScriptedProvider,
    assistant,
    input_row,
    user,
)

NOW = "2026-10-04T00:00:00.000000Z"
SESSION = "session"
_AUDIT_KINDS = {"model_attempt": "attempt", "context_checkpoint_turn": "checkpoint_turn"}


class _Fake:
    def __init__(self) -> None:
        self.store = InMemoryTranscriptStore()
        self._ids = 0

    def stored_input(self, session_id: str) -> str:
        self._ids += 1
        return f"msg_{self._ids}"

    def fork(self, child: str, source: str, anchor) -> None:
        self.store.fork(Snapshot(source, anchor.context_seq, {}), session_id=child)

    def audits(self) -> list[tuple[str, dict]]:
        return [("checkpoint_turn", payload) for _, _, payload in self.store.audits] + [
            ("attempt", payload) for _, _, payload in self.store.attempts
        ]

    def close(self) -> None:
        pass


class _SQLite:
    """A tmp state database (the suite's isolated home) with one Session, and its store or the adapter's."""

    def __init__(self, *, adapter: bool) -> None:
        ensure_sqlite_state()
        self.engine = create_sqlite_engine()
        with self.engine.begin() as conn:
            self.scope = upsert_scope(conn, platform="slack", scope_type="channel", native_id="contract", now=NOW)
            _session(conn, SESSION, self.scope)
        store = SQLiteTranscriptStore(self.engine)
        self.store = (
            AdapterTranscriptStore(store, self.engine, environment=lambda _: {}, on_response=lambda *_: None)
            if adapter
            else store
        )

    def stored_input(self, session_id: str) -> str:
        """An input row as the Delivery path stores it, before the loop consumes it."""
        with self.engine.begin() as conn:
            row = messages_service.append(
                conn,
                scope_id=self.scope,
                session_id=session_id,
                platform="slack",
                author="user",
                source="user",
                message_type="user",
                text="input",
            )
        return row["id"]

    def fork(self, child: str, source: str, anchor) -> None:
        with self.engine.begin() as conn:
            metadata = {
                "created_via": "session_fork",
                "fork_source_session_id": source,
                "fork_source_message_id": anchor.row_id,
                "fork_source_context_seq": resolve_fork_anchor_seq(conn, source, anchor.row_id),
            }
            _session(conn, child, self.scope, metadata)

    def audits(self) -> list[tuple[str, dict]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(agent_events.c.event_type, agent_events.c.content_json, agent_events.c.context_seq).where(
                    agent_events.c.visibility == "audit"
                )
            ).all()
        assert all(row.context_seq is None for row in rows)
        return [(_AUDIT_KINDS[row.event_type], json.loads(row.content_json)) for row in rows]

    def close(self) -> None:
        self.engine.dispose()


def _session(conn, session_id: str, scope_id: str, metadata: dict | None = None) -> None:
    conn.exec_driver_sql(
        "insert into agent_sessions (id, scope_id, agent_name, agent_backend, agent_variant, session_anchor, "
        "workdir, native_session_id, status, visibility, pinned, agent_status, metadata_json, created_at, "
        "updated_at, last_active_at) values (?, ?, 'avibe', 'avibe', 'avibe', ?, '/tmp', ?, 'active', "
        "'foreground', 0, 'idle', ?, ?, ?, ?)",
        (session_id, scope_id, session_id, session_id, json.dumps(metadata or {}), NOW, NOW, NOW),
    )


def _backend(name: str):
    return _Fake() if name == "fake" else _SQLite(adapter=name == "adapter")


@pytest.fixture(params=["fake", "sqlite", "adapter"])
def backend(request):
    backend = _backend(request.param)
    yield backend
    backend.close()


async def _started(backend):
    """A Session that has consumed one input: every later context row answers a Turn."""
    return await backend.store.consume_input(SESSION, backend.stored_input(SESSION), user("hello"))


def _result(call_id: str, value: str) -> ToolResultMessage:
    return ToolResultMessage(call_id, "read", (text(value),))


# --- the contract ------------------------------------------------------------------------------------


def test_the_store_implements_every_protocol_method_with_its_parameters(backend):
    declared = {name: member for name, member in vars(TranscriptStore).items() if inspect.iscoroutinefunction(member)}
    assert declared  # the protocol is read, not assumed
    for name, method in declared.items():
        implemented = getattr(backend.store, name, None)
        assert implemented is not None and inspect.iscoroutinefunction(implemented), name
        accepted = inspect.signature(implemented).parameters
        wanted = list(inspect.signature(method).parameters.values())[1:]
        for parameter in wanted:
            assert parameter.name in accepted, (name, parameter.name)
            assert accepted[parameter.name].kind == parameter.kind, (name, parameter.name)
            # An optional protocol parameter is optional in the store too.
            assert (accepted[parameter.name].default is inspect.Parameter.empty) == (
                parameter.default is inspect.Parameter.empty
            ), (name, parameter.name)
        names = {parameter.name for parameter in wanted}
        extra = [p for p in accepted.values() if p.name not in names and p.default is inspect.Parameter.empty]
        assert not extra, (name, extra)  # anything more the store takes is optional


async def test_each_append_returns_the_entry_load_reads_back_in_commit_order(backend):
    store = backend.store
    call = ToolCallBlock("call-1", "read", {"path": "f"})
    before = time.time()
    written = [
        await _started(backend),
        await store.append_response(SESSION, assistant(calls=[call]), final=False, request={"tokens": 120}),
        await store.append_tool_result(SESSION, _result("call-1", "body"), details={"path": "f"}),
        await store.append_payload(SESSION, "agent_state", {"version": 1, "state": {}}),
        await store.append_response(SESSION, assistant("done"), final=True),
    ]
    after = time.time()
    loaded = list(await store.load(SESSION))
    assert loaded == written
    assert [entry.kind for entry in loaded] == ["input", "response", "tool_result", "agent_state", "response"]
    assert [entry.context_seq for entry in loaded] == list(range(1, 6))
    assert [entry.message for entry in loaded if entry.message is not None] == [
        user("hello"),
        assistant(calls=[call]),
        _result("call-1", "body"),
        assistant("done"),
    ]
    # A response keeps the request facts it was sent with (C-9 section 2), and only those.
    assert loaded[1].payload["request"] == {"tokens": 120} and "request" not in loaded[4].payload
    assert loaded[2].payload["details"] == {"path": "f"}
    # Every row carries its commit time, in epoch seconds, in commit order (C-9 section 3 reads it).
    times = [entry.created_at for entry in loaded]
    assert all(isinstance(value, float) and before - 0.001 <= value <= after + 0.001 for value in times), times
    assert times == sorted(times)


async def test_a_payload_row_needs_a_payload_kind_and_the_current_version(backend):
    store = backend.store
    before = [await _started(backend)]
    with pytest.raises(ValueError):
        await store.append_payload(SESSION, "tool_result", {"version": 1})
    with pytest.raises(ValueError):
        await store.append_payload(SESSION, "agent_state", {"state": {}})
    assert list(await store.load(SESSION)) == before


async def test_append_payloads_commits_every_entry_in_order_or_none(backend):
    store = backend.store
    before = [await _started(backend)]
    batch = [
        ("context_edit", {"version": 1, "edit": 1}),
        ("compaction", {"version": 1, "summary": "s"}),
        ("agent_state", {"version": 1, "state": {}}),
    ]
    written = list(await store.append_payloads(SESSION, batch))
    assert [(entry.kind, dict(entry.payload)) for entry in written] == batch
    assert [entry.context_seq for entry in written] == [2, 3, 4]
    assert list(await store.load(SESSION)) == [*before, *written]
    for invalid in (
        [("context_edit", {"version": 1}), ("compaction", {"summary": "no version"})],
        [("agent_state", {"version": 1, "state": {}}), ("tool_result", {"version": 1})],
    ):
        with pytest.raises(ValueError):
            await store.append_payloads(SESSION, invalid)
    assert list(await store.load(SESSION)) == [*before, *written]  # nothing of a rejected batch landed


async def test_audit_rows_are_kept_outside_the_context(backend):
    store = backend.store
    before = [await _started(backend)]
    attempt = {"version": 1, "usage": {"input_tokens": 5, "output_tokens": 2}, "error": "rate_limit: busy"}
    turn = {"version": 1, "reason": "threshold", "mode": "normal", "messages": []}
    ids = [await store.append_audit(SESSION, "attempt", attempt), await store.append_audit(SESSION, "checkpoint_turn", turn)]
    assert all(isinstance(row_id, str) and row_id for row_id in ids) and len(set(ids)) == 2
    with pytest.raises(ValueError):
        await store.append_audit(SESSION, "compaction", {"version": 1})
    assert list(await store.load(SESSION)) == before
    assert sorted(backend.audits(), key=repr) == sorted([("attempt", attempt), ("checkpoint_turn", turn)], key=repr)


async def test_consuming_an_input_again_returns_its_entry_and_never_rewrites_it(backend):
    store = backend.store
    message_id = backend.stored_input(SESSION)
    first = await store.consume_input(SESSION, message_id, user("hello"))
    assert await store.consume_input(SESSION, message_id, user("hello")) == first
    with pytest.raises((ValueError, RuntimeError)):
        await store.consume_input(SESSION, message_id, user("changed"))
    assert list(await store.load(SESSION)) == [first]


async def test_a_call_is_settled_once(backend):
    store = backend.store
    await _started(backend)
    await store.append_response(SESSION, assistant(calls=[ToolCallBlock("call-1", "read", {"path": "f"})]), final=False)
    first = await store.append_tool_result(SESSION, _result("call-1", "one"), details={})
    assert await store.append_tool_result(SESSION, _result("call-1", "two"), details={}) == first
    assert [entry for entry in await store.load(SESSION) if entry.kind == "tool_result"] == [first]


async def test_a_fork_loads_its_source_up_to_the_anchor_then_its_own_rows(backend):
    store = backend.store
    await _started(backend)
    anchor = await store.append_response(SESSION, assistant("one"), final=True)
    await store.consume_input(SESSION, backend.stored_input(SESSION), user("later"))
    await store.append_response(SESSION, assistant("two"), final=True)
    backend.fork("child", SESSION, anchor)
    inherited = [entry for entry in await store.load(SESSION) if entry.context_seq <= anchor.context_seq]
    assert list(await store.load("child")) == inherited
    own = await store.consume_input("child", backend.stored_input("child"), user("child"))
    assert own.context_seq == anchor.context_seq + 1
    assert list(await store.load("child")) == [*inherited, own]


async def test_a_failure_inside_the_transaction_commits_none_of_the_batch(monkeypatch):
    # SQLite only: a database error after the first row is inserted rolls the whole batch back.
    backend = _SQLite(adapter=False)
    try:
        before = [await _started(backend)]
        original = agent_events_service.append
        calls = []

        def failing(*args, **kwargs):
            calls.append(kwargs["event_type"])
            if len(calls) == 2:
                raise RuntimeError("disk I/O error")
            return original(*args, **kwargs)

        monkeypatch.setattr(agent_events_service, "append", failing)
        with pytest.raises(RuntimeError):
            await backend.store.append_payloads(
                SESSION, [("context_edit", {"version": 1}), ("agent_state", {"version": 1, "state": {}})]
            )
        assert calls == ["context_edit", "agent_state"]
        assert list(await backend.store.load(SESSION)) == before
    finally:
        backend.close()


# --- C-9 through the loop, on the fake and on SQLite ---------------------------------------------------


@pytest.fixture(params=["fake", "sqlite"])
def c9(request):
    backend = _backend(request.param)
    yield backend
    backend.close()


async def test_a_new_agent_anchors_on_the_stored_request_facts(c9):
    # The second Turn's request is past T by UTF-8/4, but the stored usage of the last response says it fits.
    read = billed(assistant(calls=[ToolCallBlock("r", "read", {"path": "notes.md"})]), 0.8)
    first = Model([read, billed(assistant("Summary of the notes."), 0.8)])
    await run(make_agent(first, store=c9.store, tools=[reader(english(18_000))]), english(100), row=c9.stored_input(SESSION))
    model = Model([[Done(assistant("ok"))]], [[Done(assistant(CHECKPOINT))]])
    events = await run(make_agent(model, store=c9.store), english(2_000), row=c9.stored_input(SESSION))
    assert not model.checkpoint_requests and events[-1].reason == "completed"
    request = model.conversation_requests[0]
    assert request_tokens(request.system, request.tools, request.messages) >= THRESHOLD


async def test_a_new_agent_clears_old_results_once_the_cache_has_gone_cold(c9):
    idle = [0.0]
    context = ContextConfig(clock=lambda: time.time() + idle[0])
    reads = [[Done(assistant(calls=[ToolCallBlock(f"r{index}", "read", {"path": "f"})]))] for index in range(9)]
    model = Model([*reads, [Done(assistant("one"))], [Done(assistant("two"))]])
    agent = make_agent(model, store=c9.store, tools=[reader(tokens(6_000))], selection=LARGE, context=context)
    await run(agent, row=c9.stored_input(SESSION))
    await run(agent, row=c9.stored_input(SESSION))
    assert not [row for row in await c9.store.load(SESSION) if row.kind == "context_edit"]  # warm, below 0.8 T
    idle[0] = 301  # past the cache TTL; the restarted Agent knows only the stored commit times
    model = Model([[Done(assistant("three"))]])
    restarted = make_agent(model, store=c9.store, tools=[reader(tokens(6_000))], selection=LARGE, context=context)
    await run(restarted, row=c9.stored_input(SESSION))
    edits = [row for row in await c9.store.load(SESSION) if row.kind == "context_edit"]
    assert len(edits) == 4  # nine results before the last two turns; the newest five eligible stay


async def test_a_checkpoint_commits_with_its_state_and_a_new_agent_continues_from_it(c9):
    model = Model(growing(4), [[Done(assistant(CHECKPOINT))]])
    events = await run(make_agent(model, store=c9.store, tools=[reader(tokens(6_000))]), row=c9.stored_input(SESSION))
    assert events[-1].reason == "completed"
    rows = await c9.store.load(SESSION)
    assert [row.kind for row in rows].count("compaction") == 1
    model = Model([[Done(assistant("next"))]])
    events = await run(make_agent(model, store=c9.store), "next", row=c9.stored_input(SESSION))
    assert events[-1].reason == "completed"
    sent = model.conversation_requests[0].messages
    # The cut split the first Turn: its input stays first, then the checkpoint (C-9 context.md section 5).
    assert sent[0].content[0].text == "go" and CHECKPOINT in sent[1].content[0].text
    kept = [message.tool_call_id for message in sent if isinstance(message, ToolResultMessage)]
    assert "read-0" not in kept  # what the checkpoint summarized is gone after the restart too


async def test_a_usage_only_attempt_is_one_audit_row_and_never_context_without_context_management(c9):
    partial = AssistantMessage((), assistant().origin, "error", usage=Usage(input_tokens=5, output_tokens=2))
    answer = AssistantMessage(assistant("done").content, assistant().origin, "stop", usage=Usage(input_tokens=7))
    provider = ScriptedProvider([[ProviderError("rate_limit", "busy", True, partial=partial)], [Done(answer)]])
    agent = Agent(
        session_id=SESSION,
        models=FakeModelRouter(provider),
        tools=[],
        hooks=(),
        store=c9.store,
        jobs=FakeJobHost(),
        cwd="/test-owned",
        retry=RetryPolicy(initial_delay_s=0),
    )
    events = [event async for event in agent.run(input_row(c9.stored_input(SESSION), "go"), turn_id="turn")]
    assert events[-1].reason == "completed"
    assert [row.message.usage for row in await c9.store.load(SESSION) if row.kind == "response"] == [answer.usage]
    assert [(kind, payload["usage"]) for kind, payload in c9.audits()] == [("attempt", usage_to_dict(partial.usage))]
