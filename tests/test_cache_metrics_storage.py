"""Owning tests for cache diagnostic views, permissions, and read-only CLI shape."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from config import paths
from storage.agent_transcript import CACHE_ATTEMPTS_SQL
from storage.background import SQLiteBackgroundTaskStore
from storage.pagination import PageRequest
from storage.read_only_query import ReadOnlyQueryError, run_read_only_query
from storage.cache_metrics import observation
from vibe.authorization import AuthorizationContext, instance_owner_context
from vibe.cli import build_parser


@pytest.fixture
def state_db(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    SQLiteBackgroundTaskStore().close()
    return paths.get_sqlite_state_path()


def _session(conn: sqlite3.Connection, session_id: str = "ses-cache") -> None:
    conn.execute(
        "insert into agent_sessions (id, agent_backend, agent_variant, session_anchor, native_session_id, status, "
        "metadata_json, created_at, updated_at, last_active_at, title, agent_name) "
        "values (?, 'vibey', 'default', ?, '', 'active', '{}', ?, ?, ?, 'Cache', 'vibey')",
        (session_id, session_id, "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
    )


def _response(
    conn: sqlite3.Connection,
    message_id: str,
    at: str,
    *,
    cache: dict | None,
    usage: dict | None,
    context_seq: int,
    session_id: str = "ses-cache",
    content_json: object | None = None,
) -> None:
    message = {
        "version": 1,
        "message": {
            "role": "assistant",
            "content": [],
            "origin": {"provider": "anthropic", "api": "anthropic", "model": "claude-test"},
            "stop_reason": "stop",
            **({"usage": usage} if usage is not None else {}),
        },
        **({"cache": cache} if cache is not None else {}),
    }
    body = {"model": message} if content_json is None else content_json
    conn.execute(
        "insert into messages (id, session_id, platform, author, type, content_text, content_json, metadata_json, "
        "created_at, updated_at, context_seq) values (?, ?, 'avibe', 'agent', 'result', '', ?, '{}', ?, ?, ?)",
        (message_id, session_id, json.dumps(body), at, at, context_seq),
    )


def test_cache_view_reads_observed_rows_and_ignores_scalar_optional_json(state_db: Path) -> None:
    cache = {
        "version": 1,
        "started_at": 1790812800,
        "provider": "anthropic",
        "protocol": "anthropic",
        "model": "claude-test",
        "route": "route",
        "prefix": [["hash", 100]],
        "context_seq": 1,
        "lifetime_s": 300,
        "input_tokens": 110,
        "idle_s": 10,
        "expected_read": 100,
        "expected_read_basis": "reported_input",
        "reusable_tokens": 100,
        "reusable_static_tokens": 20,
        "previous_input_tokens": 100,
        "writes_by_ttl": {"5m": 10},
        "thread_attempts": [{"mode": "continue", "body_bytes": 450, "full_bytes": 8000}],
        "successful_wires": [{"body_bytes": 450, "source_id": "src_fixture001", "source_kind": "subscription"}],
        "cache_write_basis": "reported",
        "causes": [],
    }
    with sqlite3.connect(state_db) as conn:
        _session(conn)
        _response(
            conn, "m-observed", "2026-10-01T00:00:00Z", cache=cache,
            usage={"input_tokens": 0, "output_tokens": 2, "cache_read_tokens": 100, "cache_write_tokens": 10},
            context_seq=1,
        )
        _response(conn, "m-scalar", "2026-10-01T00:01:00Z", cache=None, usage=None, context_seq=2,
                  content_json={"model": "not-an-object"})
        _response(conn, "m-invalid", "2026-10-01T00:02:00Z", cache=None, usage=None, context_seq=3,
                  content_json="not-json")
        conn.execute(
            "insert into agent_events (id, session_id, platform, event_type, visibility, content_json, metadata_json, created_at, updated_at) "
            "values ('evt-attempt', 'ses-cache', 'avibe', 'model_attempt', 'audit', ?, '{}', '2026-10-01T00:03:00Z', '2026-10-01T00:03:00Z')",
            (json.dumps({"cache": cache, "usage": {"input_tokens": 0, "cache_read_tokens": 100, "cache_write_tokens": 10}, "cache_state": [cache]}),),
        )
        conn.execute(
            "insert into agent_events (id, session_id, platform, event_type, visibility, content_json, metadata_json, created_at, updated_at) "
            "values ('evt-fork', 'ses-cache', 'avibe', 'fork_turn', 'audit', ?, '{}', '2026-10-01T00:04:00Z', '2026-10-01T00:04:00Z')",
            (json.dumps({"cache_attempts": [{"cache": cache, "usage": {"input_tokens": 0, "cache_read_tokens": 100, "cache_write_tokens": 10}}]}),),
        )
        conn.execute("update messages set content_json = 'not-json' where id = 'm-invalid'")
        conn.execute("update agent_sessions set agent_backend = 'codex' where id = 'ses-cache'")
        conn.execute("update messages set type = 'output' where id = 'm-observed'")
        conn.commit()

    result = run_read_only_query(
        "select request_id, purpose, idle_s, classification, data_basis, input_tokens, cache_write_basis, writes_by_ttl, thread_attempts "
        "from cache_requests order by request_id",
        db_path=state_db, page_request=PageRequest(), user_context=instance_owner_context(),
    )
    by_id = {row["request_id"]: row for row in result.rows}
    assert json.loads(by_id['m-observed']['thread_attempts']) == cache['thread_attempts']
    assert by_id['m-scalar']['thread_attempts'] is None
    uploads = run_read_only_query('select request_id, successful_wires from request_uploads', db_path=state_db,
                                  page_request=None, user_context=instance_owner_context()).rows
    assert {row['request_id'] for row in uploads} == set(by_id)
    for row in uploads:
        if row['request_id'] in {'m-scalar', 'm-invalid'}:
            assert row['successful_wires'] is None
        else:
            assert json.loads(row['successful_wires']) == [{**w, 'sent_at_us': None} for w in cache['successful_wires']]
    assert by_id["m-observed"]["idle_s"] == 10
    assert by_id["m-observed"]["classification"] == "hit"
    assert by_id["m-observed"]["data_basis"] == "observed"
    assert by_id["m-observed"]["cache_write_basis"] == "reported"
    assert json.loads(by_id["m-observed"]["writes_by_ttl"]) == {"5m": 10}
    assert by_id["m-scalar"]["classification"] == "unknown"
    assert by_id["m-invalid"]["classification"] == "unknown"
    purposes = {row["purpose"] for row in result.rows if row["request_id"] != "m-observed" and row["request_id"] not in {"m-scalar", "m-invalid"}}
    assert purposes == {"attempt", "fork_turn"}
    totals = run_read_only_query(
        "select json_extract(w.value, '$.source_id') as source, sum(json_extract(w.value, '$.body_bytes')) as bytes "
        "from request_uploads as r, json_each(r.successful_wires) as w group by source",
        db_path=state_db, page_request=PageRequest(), user_context=instance_owner_context(),
    )
    assert [(row['source'], row['bytes']) for row in totals.rows] == [('src_fixture001', 3 * 450)]

def _audit_fixture(conn, name, event, cache, *, session=None, at='2026-10-01T00:00:01Z'):
    conn.execute(
        "insert into agent_events (id, session_id, platform, event_type, visibility, content_json, metadata_json, created_at, updated_at) "
        "values (?, ?, 'avibe', ?, 'audit', ?, '{}', ?, ?)",
        (name, session, event, json.dumps({'cache': cache}), at, at),
    )


def test_receipt_only_uploads_do_not_change_report_or_legacy_clock(state_db):
    from datetime import datetime, timezone
    from core.handlers.cache_metrics import report

    def summary():
        return report(since=datetime(2026, 10, 1, tzinfo=timezone.utc), until=datetime(2026, 10, 2, tzinfo=timezone.utc),
                      session_id=None, model=None, user_context=instance_owner_context(), db_path=state_db)

    with sqlite3.connect(state_db) as conn:
        _session(conn)
        for i in (0, 2):
            _response(conn, f'm-{i}', f'2026-10-01T00:00:0{i}Z', cache=None, usage={'input_tokens': 100}, context_seq=i+1)
    expected = summary()
    assert expected['requests'] == 2
    with sqlite3.connect(state_db) as conn:
        for i, kind in enumerate(('subscription', 'relay_key')):
            _audit_fixture(conn, f'bot-{i}', 'bot_handle_request', {'version': 1, 'successful_wires': [
                {'body_bytes': i+1, 'source_kind': kind, 'source_id': f'src_fixture00{i}'}]})
        _audit_fixture(conn, 'cancel-cleanup', 'model_attempt', {'version': 1, 'receipt_only': True,
                       'provider': 'anthropic', 'model': 'claude-test', 'protocol': 'anthropic', 'successful_wires': [
                           {'body_bytes': 8, 'source_kind': 'unknown', 'source_id': None}]}, session='ses-cache')
    assert summary() == expected
    assert len(_documented_upload_totals(state_db)) == 3


@pytest.mark.parametrize('after_cutoff_us', [0, 17], ids=['followup-at-cutoff', 'first-completes-after-cutoff'])
async def test_install_day_uses_each_dispatch_not_terminal_or_row_time(state_db, monkeypatch, after_cutoff_us):
    import httpx
    from core.agent_core.ai import _common
    from dataclasses import replace
    from tests.agent_core.ai.test_hosted_pause_turn import _request, _stream, _response as stream_body, _served, _text

    install_us, day_us = 1790861400123456, 86_400_000_000
    cutoff = install_us + day_us
    clock = [cutoff - 1]
    monkeypatch.setattr(_common.time, 'time_ns', lambda: clock[0] * 1000)
    req = replace(_request(), successful_wires=[])

    def answer(n):
        # Transport sees the POST first; reading the response and later persistence are not its send time.
        clock[0] = cutoff + after_cutoff_us if n == 1 else cutoff + day_us + 9
        return httpx.Response(200, headers=_served(), text=stream_body(_text('hello'),
                              stop_reason='pause_turn' if n == 1 else 'end_turn', input_tokens=1, output_tokens=1))

    sent, _ = await _stream(answer, req)
    assert [w.get('sent_at_us') for w in req.successful_wires] == [cutoff - 1, cutoff + after_cutoff_us]
    with sqlite3.connect(state_db) as conn:
        _session(conn)
        _response(conn, 'late-row', '2026-12-31T00:00:00Z', cache={'successful_wires': req.successful_wires}, usage=None, context_seq=1)
        untimed = dict(req.successful_wires[0], body_bytes=10)
        untimed.pop('sent_at_us')
        _audit_fixture(conn, 'legacy', 'bot_handle_request', {'successful_wires': [untimed, dict(untimed, sent_at_us='yesterday')]})
    doc = Path(__file__).resolve().parents[1] / 'docs/plans/cache-metrics-implementation.md'
    sql = doc.read_text().split('## Successful request uploads')[1].split('```sql\n')[2].split('```')[0]
    rows = run_read_only_query(sql, db_path=state_db, page_request=None, user_context=instance_owner_context()).rows
    assert [(r['completed_posts'], r['uploaded_bytes']) for r in rows] == [(2, sum(len(w.content) for w in sent))]
    for day, wire in enumerate(sent):
        statement = sql.replace('VALUES (1790861400123456, 0, 3)', f'VALUES ({install_us}, {day}, 1)')
        rows = run_read_only_query(statement, db_path=state_db, page_request=None, user_context=instance_owner_context()).rows
        assert [(r['completed_posts'], r['uploaded_bytes']) for r in rows] == [(1, len(wire.content))]
    assert sum(r['uploaded_bytes'] for r in _documented_upload_totals(state_db)) == sum(len(w.content) for w in sent) + 20


def _documented_upload_totals(db):
    doc = Path(__file__).resolve().parents[1] / 'docs/plans/cache-metrics-implementation.md'
    sql = doc.read_text().split('## Successful request uploads')[1].split('```sql\n')[1].split('```')[0]
    return run_read_only_query(sql, db_path=db, page_request=None, user_context=instance_owner_context()).rows


def test_documented_upload_aggregate_accepts_only_valid_receipts(state_db):
    valid = {'body_bytes': 31, 'source_kind': 'relay_key', 'source_id': 'src_fixture001'}
    with sqlite3.connect(state_db) as conn:
        for i, value in enumerate((None, {}, 'not-json', json.dumps([valid]), [], ['bad'], [
            valid, 'bad', False, None, {}, {**valid, 'body_bytes': True}, {**valid, 'body_bytes': '2'},
            {**valid, 'body_bytes': -1}, {**valid, 'body_bytes': 0}, {**valid, 'source_kind': ['wrong']},
            {**valid, 'source_id': 'not-an-id'}, {**valid, 'body_bytes': 19},
        ])):
            _audit_fixture(conn, f'bot-{i}', 'bot_handle_request', {'successful_wires': value})
    normalized = run_read_only_query('select request_id, successful_wires from request_uploads', db_path=state_db,
                                     page_request=None, user_context=instance_owner_context()).rows
    by_id = {r['request_id']: r['successful_wires'] for r in normalized}
    assert all(by_id[f'bot-{i}'] is None for i in (0, 1, 2, 3, 5))
    assert by_id['bot-4'] == '[]'
    assert _documented_upload_totals(state_db) == [
        {'kind': 'relay_key', 'source': 'src_fixture001', 'completed_posts': 2, 'uploaded_bytes': 50}]


@pytest.mark.parametrize('ending,proposal', [('success', 'bird'), ('invalid_handle', None), ('partial', None)])
async def test_sessionless_bot_proposal_appears_once_in_wire_query(state_db, monkeypatch, ending, proposal):
    from types import SimpleNamespace
    import httpx
    from core.bot_handles import propose_handle_with_model
    from core.agent_core.ai.openai_chat import OpenAIChatAdapter
    from modules.agents.model_hub import ModelHubLaunch
    from modules.agents.vibey import models
    from storage.db import create_sqlite_engine

    class Runtime:
        async def resolve(self, *args, **kwargs):
            return ModelHubLaunch(backend='vibey', channel='hub', requested_model='m', target_model='m', runtime_model='m',
                                  source_id='src_requested01', source_identity='fixture', gateway_base_url='https://hub.invalid',
                                  gateway_token='fixture', protocol='openai_chat', provider='openai')

    sent = []
    def respond(wire):
        sent.append(len(wire.content))
        answer = 'two separate words!' if ending == 'invalid_handle' else 'bird'
        body = 'data: ' + json.dumps({'choices': [{'delta': {'content': answer}}]}) + '\n\n'
        if ending != 'partial':
            body += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, headers={'x-avibe-source-kind': 'relay_key', 'x-avibe-served-hop': json.dumps(
            {'source': 'src_served0001', 'model': 'm', 'api': 'openai_chat', 'provider': 'openai'})}, text=body)

    engine = create_sqlite_engine(state_db)
    store = SimpleNamespace(engine=engine, get_main_bot_agent=lambda: SimpleNamespace(model='m'))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(models, 'registry_providers', lambda **_: lambda _: OpenAIChatAdapter(client, gateway=True))
            assert await propose_handle_with_model(SimpleNamespace(model_hub_runtime=Runtime(), vibe_agent_store=store),
                                                   display_name='小鸟') == proposal
        rows = run_read_only_query('select session_id, purpose, successful_wires from request_uploads', db_path=state_db,
                                   page_request=PageRequest(), user_context=instance_owner_context()).rows
        assert len(sent) == 1
        if rows:
            assert type(json.loads(rows[0]['successful_wires'])[0]['sent_at_us']) is int
        from unittest.mock import ANY
        assert [(r['session_id'], r['purpose'], json.loads(r['successful_wires'])) for r in rows] == ([] if ending == 'partial' else [
            (None, 'bot_handle', [{'body_bytes': sent[0], 'source_id': 'src_served0001', 'source_kind': 'relay_key', 'sent_at_us': ANY}])])
    finally:
        engine.dispose()


def test_cache_view_requires_member_access_for_audit_union(state_db: Path) -> None:
    with sqlite3.connect(state_db) as conn:
        _session(conn)
        conn.commit()
    editor = AuthorizationContext(instance_role="editor")
    for view in ('cache_requests', 'request_uploads'):
        with pytest.raises(ReadOnlyQueryError, match="agent_events"):
            run_read_only_query(f"select * from {view}", db_path=state_db, page_request=PageRequest(), user_context=editor)


def test_cache_attempt_recovery_uses_the_session_type_index_without_sorting_history(state_db: Path) -> None:
    with sqlite3.connect(state_db) as conn:
        _session(conn)
        for index in range(3):
            conn.execute(
                "insert into agent_events (id, session_id, platform, event_type, visibility, content_json, "
                "metadata_json, created_at, updated_at) values (?, ?, 'avibe', 'model_attempt', 'audit', ?, '{}', ?, ?)",
                (f"evt-{index}", "ses-cache", json.dumps({"cache_state": []}), f"2026-10-01T00:0{index}:00Z", f"2026-10-01T00:0{index}:00Z"),
            )
        plan = [
            row[3]
            for row in conn.execute(
                "explain query plan " + CACHE_ATTEMPTS_SQL.replace(":session_id", "'ses-cache'").replace(":event_type", "'model_attempt'")
            )
        ]
    assert any("ix_agent_events_session_type_created_id" in detail for detail in plan), plan
    assert not any("TEMP B-TREE" in detail for detail in plan), plan


def test_partial_prefix_expectations_are_not_called_provider_misses() -> None:
    facts = json.loads(
        observation(
            json.dumps({"version": 1, "started_at": 10, "lifetime_s": 300, "expected_read": 100,
                        "expected_read_basis": "estimated_prefix", "reusable_tokens": 100,
                        "reusable_static_tokens": 10, "previous_input_tokens": 100,
                        "writes_by_ttl": {}, "causes": []}),
            json.dumps({"input_tokens": 1, "cache_read_tokens": 50, "cache_write_tokens": 0}),
            "anthropic", "anthropic", "claude-test", 10, 11, 100,
        )
    )
    assert facts["classification"] == "estimated_miss"


def test_data_cache_parser_accepts_documented_filters():
    args = build_parser().parse_args(
        ["data", "cache", "--since", "6h", "--until", "2026-10-01T01:00:00Z", "--session-id", "ses-cache", "--model", "claude-test"]
    )
    assert args.data_command == "cache"
    assert args.since == "6h"
    assert args.until.endswith("Z")
    assert args.session_id == "ses-cache"
    assert args.model == "claude-test"


@pytest.mark.parametrize("usage,causes,idle,expected,basis,classification", [
    ({}, [], 10, 2000, "reported_input", "unknown"),
    ({}, [], 500, 0, "expired", "unknown"),
    ({"cache_read_tokens": 2000}, [{"kind": "fork_start"}], 10, 2000, "reported_input", "hit"),
    ({"cache_write_tokens": 2000}, [{"kind": "prompt_applied", "reason": "session_start"}], None, 0, "no_previous_request", "cold_start"),
    ({"cache_write_tokens": 2000}, [{"kind": "tools_changed"}], 10, 0, "estimated_prefix", "active_break"),
    ({"cache_write_tokens": 2000}, [], 500, 0, "expired", "passive_expiry"),
    ({"cache_read_tokens": 1000, "cache_write_tokens": 1000}, [], 10, 2000, "reported_input", "provider_miss"),
    ({"cache_read_tokens": 1900, "cache_write_tokens": 100}, [], 10, 2000, "reported_input", "hit"),
])
def test_classification_preserves_unknown_usage_informational_causes_and_provider_tolerance(
    usage, causes, idle, expected, basis, classification,
):
    facts = json.loads(observation(
        {"version": 1, "started_at": 1000, "lifetime_s": 300, "idle_s": idle,
         "expected_read": expected, "expected_read_basis": basis,
         "previous_input_tokens": 2000, "causes": causes}, usage,
        "anthropic", "anthropic", "claude-test", 1000, 999, 2000,
    ))
    assert facts["classification"] == classification
    assert facts["idle_s"] == idle
