"""Storage-boundary contracts for checkpoint capsules (all SQLite state is tmp).

The historical reader is the differential oracle for model projection/anchors;
decoder IDs prove the optimized reader really excludes discarded canonical rows.
These risks cannot be covered by the pure retained-row builder's unit tests.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import event, select

from core.agent_core.harness.cache_metrics import CacheTracker
from core.agent_core.harness.context import last_anchor
from core.agent_core.harness.projection import ProjectionError, context_view, open_tool_calls, project
from core.agent_core.messages import Origin, ToolCallBlock, text
from storage import agent_transcript
from storage.agent_runtime_transcript import CAPSULE_KEY, _digest, read_runtime
from storage.agent_transcript import SQLiteTranscriptStore
from storage.models import agent_events
from tests.test_agent_transcript import (
    COMPACTION, ORIGIN, _assistant, _fork, _row, _scope, _session, _tool_result, _user,
    engine,  # noqa: F401 -- shared hermetic SQLite fixture
)


def _read(engine, session="ses_main", *, publish=True):
    with engine.begin() as conn:
        conn.exec_driver_sql("BEGIN")
        runtime, publications = read_runtime(conn, session)
    if publish:
        with engine.begin() as conn:
            for row_id, expected, metadata in publications:
                result = conn.execute(
                    agent_events.update().where(
                        agent_events.c.id == row_id, agent_events.c.metadata_json == expected
                    ).values(metadata_json=metadata)
                )
                assert result.rowcount == 1
    return runtime, publications


def _decodes(monkeypatch):
    ids = []
    for name in ("_message_entry", "_event_entry"):
        original = getattr(agent_transcript, name)

        def decode(session, row, original=original):
            ids.append(row["id"])
            return original(session, row)

        monkeypatch.setattr(agent_transcript, name, decode)
    return ids


def _assert_equivalent(canonical, runtime):
    assert project(runtime.entries) == project(canonical)
    assert last_anchor(runtime.entries, context_view(runtime.entries)) == last_anchor(canonical, context_view(canonical))
    assert open_tool_calls(runtime.entries) == open_tool_calls(canonical)
    clocks = {}
    for row in canonical:
        if row.kind == "response" and row.created_at is not None:
            key = (row.message.origin.provider, row.message.origin.model)
            clocks[key] = max(clocks.get(key, row.created_at), row.created_at)
    assert runtime.response_clocks == clocks
    tracker = CacheTracker()
    tracker.restore(canonical, ())
    assert list(runtime.cache_records) == [tracker.latest[key] for key in sorted(tracker.latest)]


def _cache(started, seq, *, origin=ORIGIN):
    return {"version": 1, "provider": origin.provider, "model": origin.model, "route": "route",
            "started_at": started, "context_seq": seq, "prefix": [["hash", 10, 0]], "input_tokens": 12}


async def _input(engine, store, scope, body, *, session="ses_main"):
    with engine.begin() as conn:
        row = _row(conn, session, scope, body)
    return await store.consume_input(session, row, _user(body))


async def _checkpoint(store, floor, *, session="ses_main", legacy=False):
    payload = {**COMPACTION, "first_kept_seq": floor, "summarized_to_seq": floor - 1}
    if not legacy:
        payload["memo"] = []
    return await store.append_payload(session, "compaction", payload)


async def _history(engine, *, anchor=True, legacy=False):
    with engine.begin() as conn:
        scope = _scope(conn, "C-runtime")
        _session(conn, "ses_main", scope)
    store = SQLiteTranscriptStore(engine)
    discarded = []
    for index in range(6):
        discarded.append(await _input(engine, store, scope, f"old input {index}"))
        origin = Origin(provider="archived-provider", api="anthropic", model="archived") if index == 0 else ORIGIN
        discarded.append(await store.append_response(
            "ses_main", replace(_assistant(f"old response {index}"), origin=origin), final=True,
            cache=_cache(5, discarded[-1].context_seq, origin=origin) if index == 0 else None,
        ))
    await store.append_payload("ses_main", "agent_state", {"version": 1, "state": {"mode": "plan"}})
    await store.append_payload(
        "ses_main", "replay_refusal", {"version": 1, "source_identity": "route", "payloads": ["signature"]}
    )
    current = await _input(engine, store, scope, "current input")
    response = await store.append_response(
        "ses_main", _assistant("current response"), final=True,
        request={"tokens": 70} if anchor else None, cache=_cache(10, current.context_seq),
    )
    checkpoint = await _checkpoint(store, current.context_seq, legacy=legacy)
    return store, scope, discarded, response, checkpoint


async def test_first_checkpoint_anchor_is_optional_after_new_response(engine, monkeypatch):
    # Regression: storing only one closure permanently decodes first-checkpoint
    # history even after a new response has replaced that old usage anchor.
    store, _, discarded, _, checkpoint = await _history(engine)
    canonical = await store.load("ses_main")
    first, publications = _read(engine)
    _assert_equivalent(canonical, first)
    assert len(publications) == 1
    metadata = json.loads(publications[0][2])[CAPSULE_KEY]
    assert metadata["anchor_refs"]
    assert not any(row.row_id in {ref[3] for ref in metadata["base_refs"]} for row in discarded)
    # Without a replacement anchor, its pre-checkpoint request must remain exact.
    warm, publications = _read(engine)
    assert not publications
    _assert_equivalent(canonical, warm)
    await store.append_response("ses_main", _assistant("after checkpoint"), final=True, request={"tokens": 40})
    canonical = await store.load("ses_main")
    decoded = _decodes(monkeypatch)
    warm, publications = _read(engine)
    _assert_equivalent(canonical, warm)
    assert not publications
    assert checkpoint.row_id in decoded
    assert set(decoded).isdisjoint(row.row_id for row in discarded)
    assert len(decoded) < len(canonical) // 2


async def test_new_checkpoint_derives_from_predecessor_without_lifetime_decode(engine, monkeypatch):
    store, scope, discarded, _, old = await _history(engine, anchor=False)
    _read(engine)
    current = await _input(engine, store, scope, "next input")
    await store.append_response("ses_main", _assistant("next response"), final=True, request={"tokens": 30})
    checkpoint = await _checkpoint(store, current.context_seq)
    canonical = await store.load("ses_main")
    decoded = _decodes(monkeypatch)
    runtime, publications = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert [item[0] for item in publications] == [checkpoint.row_id]
    assert old.row_id in decoded
    assert set(decoded).isdisjoint(row.row_id for row in discarded)
    decoded.clear()
    assert not _read(engine)[1]
    assert set(decoded).isdisjoint(row.row_id for row in discarded)


async def test_nested_fork_caps_checkpoint_and_response_facts(engine, monkeypatch):
    # Parent post-fork responses/audits must never become inheritable scalar facts.
    store, scope, discarded, _, checkpoint = await _history(engine, anchor=False)
    _read(engine)
    with engine.begin() as conn:
        _fork(conn, "ses_child", scope, "ses_main", as_of=checkpoint.context_seq)
    child_input = await _input(engine, store, scope, "child", session="ses_child")
    child_response = await store.append_response("ses_child", _assistant("child reply"), final=True)
    with engine.begin() as conn:
        _fork(conn, "ses_grandchild", scope, "ses_child", as_of=child_response.context_seq)
    other_origin = Origin(provider="other", api="anthropic", model="other-model")
    excluded = await store.append_response(
        "ses_main", replace(_assistant("parent tail"), origin=other_origin), final=True,
        cache=_cache(999, checkpoint.context_seq, origin=other_origin),
    )
    await _checkpoint(store, excluded.context_seq)
    await store.append_audit("ses_child", "attempt", {"cache_state": [_cache(9999, child_input.context_seq)]})
    await store.append_response("ses_child", _assistant("child excluded tail"), final=True,
                                cache=_cache(8888, child_response.context_seq))
    canonical = await store.load("ses_grandchild")
    decoded = _decodes(monkeypatch)
    runtime, publications = _read(engine, "ses_grandchild")
    _assert_equivalent(canonical, runtime)
    assert not publications
    assert excluded.row_id not in decoded
    assert set(decoded).isdisjoint(row.row_id for row in discarded)
    assert all(key[0] != "other" for key in runtime.response_clocks)
    assert runtime.cache_records[0]["started_at"] == 10


@pytest.mark.parametrize("damage", ["json", "version", "checksum", "missing", "out_of_bound", "wrong_kind", "facts"])
async def test_optional_capsule_damage_repairs_once_preserving_other_metadata(engine, monkeypatch, damage):
    store, _, discarded, _, checkpoint = await _history(engine, anchor=False)
    _, publications = _read(engine)
    metadata = json.loads(publications[0][2])
    metadata["unrelated"] = {"keep": True}
    capsule = metadata[CAPSULE_KEY]
    if damage == "version":
        capsule["version"] = 999
    elif damage == "checksum":
        capsule["digest"] = "bad"
    elif damage in {"missing", "out_of_bound", "wrong_kind"}:
        ref = capsule["base_refs"][0]
        if damage == "missing":
            ref[3] = "missing-row"
        elif damage == "out_of_bound":
            ref[2] = checkpoint.context_seq + 1
        else:
            ref[0] = "response"
    elif damage == "facts":
        capsule["response_clocks"] = [["provider", "model", "not-a-clock"]]
    if damage != "checksum":
        capsule["digest"] = _digest({key: value for key, value in capsule.items() if key != "digest"})
    raw = "{" if damage == "json" else json.dumps(metadata)
    with engine.begin() as conn:
        conn.execute(agent_events.update().where(agent_events.c.id == checkpoint.row_id).values(metadata_json=raw))
    canonical = await store.load("ses_main")
    decoded = _decodes(monkeypatch)
    runtime, repaired = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert len(repaired) == 1
    assert repaired[0][1] == raw
    if damage != "json":
        assert json.loads(repaired[0][2])["unrelated"] == {"keep": True}
    assert discarded[0].row_id in decoded
    decoded.clear()
    runtime, repaired = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert not repaired
    assert set(decoded).isdisjoint(row.row_id for row in discarded)


async def test_suffix_edit_repairs_actual_reused_call_batch_once(engine, monkeypatch):
    # A late edit names an old row, not the latest response reusing its call id.
    # Persist the target's complete batch, original bodies and chronological edits.
    with engine.begin() as conn:
        scope = _scope(conn, "C-edit-runtime")
        _session(conn, "ses_main", scope)
    store = SQLiteTranscriptStore(engine)
    first_input = await _input(engine, store, scope, "first")
    first_owner = await store.append_response("ses_main", _assistant("first", call_id="reused"), final=False)
    first_result = await store.append_tool_result("ses_main", _tool_result("reused", "first body"), details={})
    target_owner = await store.append_response(
        "ses_main", replace(_assistant("target", call_id="reused"), content=(
            text("target"), ToolCallBlock("reused", "bash", {"command": "ls"}),
            ToolCallBlock("sibling", "bash", {"command": "pwd"}),
        )), final=False,
    )
    target = await store.append_tool_result("ses_main", _tool_result("reused", "canonical target"), details={"x": 1})
    sibling = await store.append_tool_result("ses_main", _tool_result("sibling", "sibling body"), details={})
    edit_payload = {"version": 1, "target_event_id": target.row_id, "replacement": {"text": "old edit"},
                    "reason": "fit_tool_result"}
    old_edit = await store.append_payload("ses_main", "context_edit", edit_payload)
    current = await _input(engine, store, scope, "current")
    await store.append_response("ses_main", _assistant("current", call_id="reused"), final=False)
    await store.append_tool_result("ses_main", _tool_result("reused", "current body"), details={})
    checkpoint = await _checkpoint(store, current.context_seq)
    _read(engine)
    late_edit = await store.append_payload(
        "ses_main", "context_edit", {**edit_payload, "replacement": {"text": "late edit"}}
    )
    canonical = await store.load("ses_main")
    decoded = _decodes(monkeypatch)
    runtime, publications = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert first_result.row_id in decoded  # one canonical repair
    assert len(publications) == 1
    refs = json.loads(publications[0][2])[CAPSULE_KEY]["base_refs"]
    ids = {ref[3] for ref in refs}
    assert {target_owner.row_id, target.row_id, sibling.row_id, old_edit.row_id} <= ids
    assert first_owner.row_id not in ids
    assert late_edit.row_id not in ids
    assert all(ref[2] <= checkpoint.context_seq for ref in refs)
    decoded.clear()
    runtime, publications = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert not publications
    assert first_result.row_id not in decoded
    target_row = next(row for row in runtime.entries if row.row_id == target.row_id)
    assert target_row.message.content == (text("canonical target"),)
    # A second old target must not evict the first repair or promote unrelated
    # history into the base closure.
    await store.append_payload("ses_main", "context_edit", {**edit_payload, "target_event_id": first_result.row_id})
    canonical = await store.load("ses_main")
    decoded.clear()
    runtime, publications = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert len(publications) == 1 and first_input.row_id in decoded
    decoded.clear()
    runtime, publications = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert not publications and first_input.row_id not in decoded


async def test_sibling_fork_repairs_accumulate_without_promoting_optional_anchor_history(engine, monkeypatch):
    # Both children publish repairs into the same inherited checkpoint. A repair
    # based only on the current child's suffix evicts the sibling's dependency,
    # so alternating loads repeatedly decode lifetime history and republish.
    with engine.begin() as conn:
        scope = _scope(conn, "C-sibling-repair")
        _session(conn, "ses_main", scope)
    store = SQLiteTranscriptStore(engine)
    excluded = await _input(engine, store, scope, "old input needed only by original anchor")
    targets = []
    for call_id in ("a", "b"):
        await store.append_response("ses_main", _assistant(call_id, call_id=call_id), final=False)
        targets.append(await store.append_tool_result("ses_main", _tool_result(call_id, call_id), details={}))
    current = await _input(engine, store, scope, "current")
    await store.append_response("ses_main", _assistant("old anchor"), final=True, request={"tokens": 50})
    checkpoint = await _checkpoint(store, current.context_seq)
    _, initial = _read(engine)
    initial_capsule = json.loads(initial[0][2])[CAPSULE_KEY]
    assert excluded.row_id in {ref[3] for ref in initial_capsule["anchor_refs"]}
    canonicals = {}
    for session, target in zip(("ses_a", "ses_b"), targets):
        with engine.begin() as conn:
            _fork(conn, session, scope, "ses_main", as_of=checkpoint.context_seq)
        await store.append_response(session, _assistant("new anchor"), final=True, request={"tokens": 30})
        await store.append_payload(session, "context_edit", {
            "version": 1, "target_event_id": target.row_id, "replacement": {"text": "edited"},
            "reason": "fit_tool_result",
        })
        canonicals[session] = await store.load(session)
    decoded = _decodes(monkeypatch)
    for session in ("ses_a", "ses_b"):
        decoded.clear()
        runtime, publications = _read(engine, session)
        _assert_equivalent(canonicals[session], runtime)
        assert len(publications) == 1
        assert excluded.row_id in decoded  # each new dependency repairs once
        capsule = json.loads(publications[0][2])[CAPSULE_KEY]
        assert excluded.row_id not in {ref[3] for ref in capsule["base_refs"]}
    assert {row.row_id for row in targets} <= {ref[3] for ref in capsule["base_refs"]}
    for session in ("ses_a", "ses_b", "ses_a", "ses_b"):
        decoded.clear()
        runtime, publications = _read(engine, session)
        _assert_equivalent(canonicals[session], runtime)
        assert not publications
        assert excluded.row_id not in decoded


async def test_fork_before_first_checkpoint_uses_canonical_no_capsule_path(engine):
    store, scope, _, response, _ = await _history(engine)
    # The parent has a capsule, but this child's certified cut precedes it.
    _read(engine)
    with engine.begin() as conn:
        _fork(conn, "ses_before", scope, "ses_main", as_of=response.context_seq)
    canonical = await store.load("ses_before")
    runtime, publications = _read(engine, "ses_before")
    assert runtime.entries == tuple(canonical)
    _assert_equivalent(canonical, runtime)
    assert not publications


async def test_legacy_checkpoint_keeps_memo_inputs_until_first_v5_consolidation(engine, monkeypatch):
    store, scope, discarded, _, _ = await _history(engine, anchor=False, legacy=True)
    _read(engine)
    canonical = await store.load("ses_main")
    decoded = _decodes(monkeypatch)
    runtime, publications = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert not publications
    assert {row.row_id for row in discarded if row.kind == "input"} <= set(decoded)
    assert set(decoded).isdisjoint(row.row_id for row in discarded if row.kind == "response")
    current = await _input(engine, store, scope, "consolidated input")
    await _checkpoint(store, current.context_seq)
    _read(engine)
    canonical = await store.load("ses_main")
    decoded.clear()
    runtime, publications = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert not publications
    assert set(decoded).isdisjoint(row.row_id for row in discarded)


@pytest.mark.parametrize("regression", ["floor", "memo"])
async def test_checkpoint_reintroduced_dependencies_rehydrate_without_stale_prior(engine, regression):
    store, _, discarded, _, checkpoint = await _history(engine, anchor=False)
    _read(engine)
    payload = dict(checkpoint.payload)
    if regression == "floor":
        payload.update(first_kept_seq=1, summarized_to_seq=0)
    else:
        del payload["memo"]
    latest = await store.append_payload("ses_main", "compaction", payload)
    canonical = await store.load("ses_main")
    runtime, publications = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert [publication[0] for publication in publications] == [latest.row_id]
    assert discarded[0].row_id in {row.row_id for row in runtime.entries}
    runtime, publications = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert not publications


async def test_pinned_input_and_pending_call_survive_checkpoint_and_late_result(engine):
    with engine.begin() as conn:
        scope = _scope(conn, "C-late-runtime")
        _session(conn, "ses_main", scope)
    store = SQLiteTranscriptStore(engine)
    pinned = await _input(engine, store, scope, "pinned turn input")
    owner = await store.append_response("ses_main", _assistant("pending", call_id="late"), final=False)
    current = await _input(engine, store, scope, "steer")
    await store.append_payload("ses_main", "compaction", {
        **COMPACTION, "memo": [], "first_kept_seq": current.context_seq,
        "summarized_to_seq": current.context_seq - 1, "kept_inputs": [pinned.context_seq],
    })
    runtime, _ = _read(engine)
    assert {pinned.row_id, owner.row_id} <= {row.row_id for row in runtime.entries}
    assert open_tool_calls(runtime.entries)[0][0].row_id == owner.row_id
    result = await store.append_tool_result("ses_main", _tool_result("late", "late body"), details={"exit_code": 0})
    canonical = await store.load("ses_main")
    runtime, publications = _read(engine)
    _assert_equivalent(canonical, runtime)
    assert not publications
    assert not open_tool_calls(runtime.entries)
    assert next(row for row in runtime.entries if row.row_id == result.row_id) == result


async def test_canonical_corruption_raises_instead_of_becoming_cache_repair(engine):
    store, _, _, _, checkpoint = await _history(engine, anchor=False)
    _read(engine)
    await store.append_payload("ses_main", "context_edit", {
        "version": 1, "target_event_id": "never-existed", "replacement": {"text": "broken"},
        "reason": "fit_tool_result",
    })
    with pytest.raises(ProjectionError, match="targets no earlier tool result"):
        _read(engine)
    with engine.connect() as conn:
        metadata = conn.execute(select(agent_events.c.metadata_json).where(agent_events.c.id == checkpoint.row_id)).scalar()
    assert CAPSULE_KEY in json.loads(metadata)


async def test_async_runtime_publisher_cas_preserves_concurrent_metadata_and_warm_read_is_write_free(engine, monkeypatch):
    # The helper cannot cover the read/write transaction gap. Exercise the real
    # async publisher while a different writer wins that gap, then converge.
    store, _, discarded, _, checkpoint = await _history(engine, anchor=False)
    canonical = await store.load("ses_main")
    original = store._load_runtime
    unrelated = {"other_writer": {"revision": 2}}

    def read_then_race(session_id):
        snapshot = original(session_id)
        with engine.begin() as conn:
            conn.execute(agent_events.update().where(agent_events.c.id == checkpoint.row_id)
                         .values(metadata_json=json.dumps(unrelated)))
        return snapshot

    def persisted_metadata():
        with engine.connect() as conn:
            raw = conn.execute(select(agent_events.c.metadata_json)
                               .where(agent_events.c.id == checkpoint.row_id)).scalar_one()
        return json.loads(raw)

    monkeypatch.setattr(store, "_load_runtime", read_then_race)
    _assert_equivalent(canonical, await store.load_for_run("ses_main"))
    assert persisted_metadata() == unrelated  # stale capsule publication lost CAS
    monkeypatch.setattr(store, "_load_runtime", original)
    _assert_equivalent(canonical, await store.load_for_run("ses_main"))
    repaired = persisted_metadata()
    assert repaired["other_writer"] == unrelated["other_writer"]
    assert CAPSULE_KEY in repaired
    decoded = _decodes(monkeypatch)
    writes = []

    def capture_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"UPDATE", "INSERT", "DELETE", "REPLACE"}:
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", capture_write)
    try:
        _assert_equivalent(canonical, await store.load_for_run("ses_main"))
    finally:
        event.remove(engine, "before_cursor_execute", capture_write)
    assert not writes
    assert persisted_metadata() == repaired
    assert set(decoded).isdisjoint(row.row_id for row in discarded)


async def test_checkpoint_and_tail_reads_use_bounded_searches_without_temp_sort(engine):
    store, _, _, _, _ = await _history(engine, anchor=False)
    _read(engine)
    tail = await store.append_response("ses_main", _assistant("tail"), final=True)
    await _checkpoint(store, tail.context_seq)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and ("FROM agent_events" in statement or "FROM messages" in statement):
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        _read(engine)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements
    assert any("context_seq <" in sql for sql, _ in statements)  # predecessor seek as well as latest
    with engine.connect() as conn:
        plans = [(sql, " ".join(row[3] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params)))
                 for sql, params in statements]
    for sql, plan in plans:
        assert "SEARCH" in plan, (sql, plan)
        assert "SCAN" not in plan and "TEMP B-TREE" not in plan, (sql, plan)
        if "event_type =" in sql:
            assert "ix_agent_events_session_type_context_seq" in plan, plan
        if "context_seq >" in sql:
            assert "context_seq>?" in plan, plan
