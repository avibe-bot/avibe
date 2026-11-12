"""Reader-first prefix storage: no writer switch, backfill, or historical mutation.

The old tracker skipped prefixless responses and silently revived older inline
prefixes. These tests exercise the real SQLite reader and tracker together; the
existing transcript contract only covers inline records written by today's loop.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import text

from core.agent_core.harness.cache_metrics import CacheTracker
from core.agent_core.messages import AssistantMessage, Origin, TextBlock, Usage
from storage.agent_transcript import CACHE_STATE_SQL, SQLiteTranscriptStore
from storage.cache_metrics import observation
from storage.models import session_cache_state
from tests.agent_core.agent.test_cache_metrics import request
from tests.agent_core.fakes import assistant
from tests.test_transcript_store_contract import SESSION, _SQLite, _started


@pytest.fixture
async def backend():
    value = _SQLite(adapter=False)
    await _started(value)
    yield value
    value.close()


def _record(*, now=100, model=None):
    candidate = request()
    message = replace(assistant(), usage=Usage(input_tokens=100))
    if model is not None:
        candidate = replace(candidate, endpoint=replace(candidate.endpoint, model_id=model))
        message = replace(message, origin=replace(message.origin, model=model))
    tracker = CacheTracker()
    record = tracker.begin(candidate, (), now=now)
    tracker.finish(record, message)
    return candidate, message, record


def _state(backend, record, *, session_id=SESSION, provider=None, model=None, raw=None):
    # Fixtures stand for the future writer. This release has no production write
    # path for this table, and today's append bytes are checked separately below.
    with backend.engine.begin() as conn:
        conn.execute(session_cache_state.insert().values(
            session_id=session_id, provider=provider or record["provider"], model=model or record["model"],
            record_json=json.dumps(record) if raw is None else raw,
        ))


async def _restore(backend, session_id=SESSION):
    # A fresh store and tracker, as a new process/Turn would use them.
    store = SQLiteTranscriptStore(backend.engine)
    rows = await store.load(session_id)
    tracker = CacheTracker()
    tracker.restore(rows, await store.load_cache_attempts(session_id))
    return tracker


async def test_latest_route_state_restores_new_history_and_yields_to_newer_legacy_writer(backend):
    candidate, message, record = _record()
    prefixless = {key: value for key, value in record.items() if key != "prefix"}
    await backend.store.append_response(SESSION, message, final=True, cache=prefixless)
    _state(backend, record)
    other_request, other_message, other = _record(now=110, model="other-model")
    await backend.store.append_response(SESSION, other_message, final=True,
                                        cache={key: value for key, value in other.items() if key != "prefix"})
    _state(backend, other)
    restored = await _restore(backend)
    for routed in (candidate, other_request):
        following = restored.begin(routed, (), now=120)
        assert following["expected_read"] == 100
        assert following["expected_read_basis"] == "reported_input"
    assert restored.begin(candidate, (), now=120)["idle_s"] == 20
    assert restored.begin(other_request, (), now=120)["idle_s"] == 10

    # Rolling back to an older writer can leave the independent table stale.
    # Its later inline response must win when the new reader returns.
    newer = restored.begin(replace(candidate, system="changed"), (), now=130)
    restored.finish(newer, replace(message, usage=Usage(input_tokens=250)))
    await backend.store.append_response(SESSION, message, final=True, cache=newer)
    restarted = await _restore(backend)
    assert restarted.latest[(record["provider"], record["model"])] == newer
    assert restarted.begin(replace(candidate, system="changed"), (), now=140)["expected_read"] == 250


async def test_prefixless_newer_history_invalidates_stale_state_without_losing_clock(backend):
    candidate, message, old = _record()
    await backend.store.append_response(SESSION, message, final=True, cache=old)
    _state(backend, old)
    _, _, newer = _record(now=110)
    prefixless = {key: value for key, value in newer.items() if key != "prefix"}
    await backend.store.append_response(SESSION, message, final=True, cache=prefixless)
    restarted = await _restore(backend)
    following = restarted.begin(candidate, (), now=120)
    assert following["idle_s"] == 10
    assert following["previous_input_tokens"] == 100
    assert following["expected_read"] == 0
    assert following["expected_read_basis"] == "missing_prefix"
    assert following["causes"] == []

    # Missing usage never refreshes a provider-cache clock, even without prefix.
    await backend.store.append_response(SESSION, message, final=True,
                                        cache={**prefixless, "started_at": 119, "input_tokens": None})
    assert (await _restore(backend)).begin(candidate, (), now=120)["idle_s"] == 10


async def test_historical_fork_never_uses_parent_future_prefix_and_child_restores_its_own(backend):
    candidate, message, old = _record()
    await backend.store.append_response(SESSION, message, final=True, cache=old)
    _, _, at_fork = _record(now=110)
    anchor = await backend.store.append_response(
        SESSION, message, final=True, cache={key: value for key, value in at_fork.items() if key != "prefix"},
    )
    backend.fork("child", SESSION, anchor)
    _, _, future = _record(now=150)
    _state(backend, future)
    inherited = await _restore(backend, "child")
    first = inherited.begin(candidate, (), now=160, fork=True)
    assert first["idle_s"] == 50  # the fork point, not the parent's later clock
    assert first["expected_read_basis"] == "missing_prefix"
    assert first["expected_read"] == 0
    assert first["causes"] == [{"kind": "fork_start"}]
    inherited.finish(first, message)
    await backend.store.append_response("child", message, final=True,
                                        cache={key: value for key, value in first.items() if key != "prefix"})
    _state(backend, first, session_id="child")
    assert (await _restore(backend, "child")).begin(candidate, (), now=170)["expected_read"] == 100


async def test_state_reader_merges_legacy_audits_and_ignores_damaged_or_misattributed_state(backend):
    candidate, message, old = _record()
    await backend.store.append_audit(SESSION, "attempt", {"version": 1, "cache": old, "cache_state": [old]})
    _, _, newer = _record(now=110)
    _state(backend, newer)
    _state(backend, newer, model="broken-json", raw="not json")
    _state(backend, newer, model="wrong-route")
    _state(backend, newer, model="scalar", raw='"scalar"')
    _state(backend, newer, model="invalid", raw='{"version": 1}')
    restored = await _restore(backend)
    assert list(restored.latest.values()) == [newer]
    assert restored.begin(candidate, (), now=120)["idle_s"] == 10
    latest = {**newer, "started_at": 130}
    await backend.store.append_audit(SESSION, "fork_turn", {"version": 1, "cache_state": [latest]})
    assert list((await _restore(backend)).latest.values()) == [latest]


@pytest.mark.parametrize("kind", ["attempt", "fork_turn"])
async def test_prefixless_audit_without_snapshot_supersedes_older_inline_and_state(backend, kind):
    candidate, message, old = _record()
    await backend.store.append_response(SESSION, message, final=True, cache=old)
    _state(backend, old)
    _, _, newer = _record(now=110)
    prefixless = {key: value for key, value in newer.items() if key != "prefix"}
    payload = {"cache": prefixless} if kind == "attempt" else {"cache_attempts": [{"cache": prefixless}]}
    await backend.store.append_audit(SESSION, kind, {"version": 1, **payload})
    following = (await _restore(backend)).begin(candidate, (), now=120)
    assert following["idle_s"] == 10
    assert following["expected_read"] == 0
    assert following["expected_read_basis"] == "missing_prefix"


def test_prefixless_historical_metrics_are_identical():
    _, _, record = _record()
    prefixless = {key: value for key, value in record.items() if key != "prefix"}
    args = ({"input_tokens": 100}, record["provider"], "anthropic", record["model"], 100, None, None)
    assert observation(record, *args) == observation(prefixless, *args)


async def test_reader_bridge_keeps_response_and_both_audit_write_bytes_unchanged(backend):
    # Explicit pre-bridge wire shape; do not derive expected payloads from the
    # serializer under test. Prefixes and snapshots must still be written in full.
    cache = {
        "version": 1, "provider": "anthropic", "model": "claude-test", "route": "route",
        "started_at": 100, "context_seq": 0, "prefix": [["hash", 100, 0]], "input_tokens": 100,
    }
    message = AssistantMessage((TextBlock("done"),), Origin("anthropic", "anthropic", "claude-test"), "stop")
    response = await backend.store.append_response(SESSION, message, final=True, cache=cache)
    audit = {"version": 1, "cache": cache, "cache_state": [cache]}
    attempt_id = await backend.store.append_audit(SESSION, "attempt", audit)
    side = {"version": 1, "cache_attempts": [{"cache": cache}], "cache_state": [cache]}
    side_id = await backend.store.append_audit(SESSION, "fork_turn", side)
    expected = json.dumps({"model": {
        "version": 1, "message": {
            "role": "assistant", "content": [{"type": "text", "text": "done"}],
            "origin": {"provider": "anthropic", "api": "anthropic", "model": "claude-test"},
            "stop_reason": "stop",
        }, "cache": cache,
    }, "text": "done"})
    with backend.engine.connect() as conn:
        before = list(conn.execute(text("SELECT id, content_json FROM messages ORDER BY id")))
        before_audits = list(conn.execute(text("SELECT id, content_json FROM agent_events ORDER BY id")))
        assert dict(before)[response.row_id] == expected
        assert dict(before_audits)[attempt_id] == json.dumps(audit)
        assert dict(before_audits)[side_id] == json.dumps(side)
        assert conn.execute(text("SELECT count(*) FROM session_cache_state")).scalar() == 0
    await _restore(backend)
    with backend.engine.connect() as conn:
        assert list(conn.execute(text("SELECT id, content_json FROM messages ORDER BY id"))) == before
        assert list(conn.execute(text("SELECT id, content_json FROM agent_events ORDER BY id"))) == before_audits
        assert conn.execute(text("SELECT count(*) FROM session_cache_state")).scalar() == 0


def test_state_read_is_a_session_key_search_without_history_sort(backend):
    with backend.engine.connect() as conn:
        plan = [row[3] for row in conn.execute(text("EXPLAIN QUERY PLAN " + CACHE_STATE_SQL), {"session_id": SESSION})]
    assert any("SEARCH session_cache_state" in detail and "session_id=?" in detail for detail in plan), plan
    assert not any("TEMP B-TREE" in detail or "SCAN" in detail for detail in plan), plan


def test_additive_upgrade_preserves_legacy_rows_and_epoch_without_backfill(tmp_path):
    import sqlite3
    from alembic.script import ScriptDirectory
    from storage.migrations import alembic_config, run_migrations
    from storage.migration_policy import classify_migration

    path = tmp_path / "legacy.sqlite"
    revision = ScriptDirectory.from_config(alembic_config(path)).get_revision("20261112_0085")
    assert classify_migration(revision.path).permitted == "additive"
    run_migrations(path, revision=revision.down_revision)
    # Irregular whitespace is deliberate: even a JSON decode/encode rewrite
    # would fail the byte-level retention check.
    legacy = '{ "model": {"version":1, "cache":{"prefix":[["hash",100,0]]}} }'
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO messages (id, platform, author, type, content_json, metadata_json, created_at, updated_at) "
            "VALUES ('legacy', 'avibe', 'agent', 'assistant', ?, '{}', 'before', 'before')", (legacy,),
        )
        before = conn.execute("SELECT * FROM messages").fetchall()
        epoch = conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone()
    run_migrations(path)
    run_migrations(path)  # reopening must not backfill either
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM messages").fetchall() == before
        assert conn.execute("SELECT count(*) FROM session_cache_state").fetchone() == (0,)
        assert conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone() == epoch == ('2',)

