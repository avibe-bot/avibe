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
        "select request_id, purpose, idle_s, classification, data_basis, input_tokens, cache_write_basis, writes_by_ttl "
        "from cache_requests order by request_id",
        db_path=state_db, page_request=PageRequest(), user_context=instance_owner_context(),
    )
    by_id = {row["request_id"]: row for row in result.rows}
    assert by_id["m-observed"]["idle_s"] == 10
    assert by_id["m-observed"]["classification"] == "hit"
    assert by_id["m-observed"]["data_basis"] == "observed"
    assert by_id["m-observed"]["cache_write_basis"] == "reported"
    assert json.loads(by_id["m-observed"]["writes_by_ttl"]) == {"5m": 10}
    assert by_id["m-scalar"]["classification"] == "unknown"
    assert by_id["m-invalid"]["classification"] == "unknown"
    purposes = {row["purpose"] for row in result.rows if row["request_id"] != "m-observed" and row["request_id"] not in {"m-scalar", "m-invalid"}}
    assert purposes == {"attempt", "fork_turn"}

def test_cache_view_requires_member_access_for_audit_union(state_db: Path) -> None:
    with sqlite3.connect(state_db) as conn:
        _session(conn)
        conn.commit()
    editor = AuthorizationContext(instance_role="editor")
    with pytest.raises(ReadOnlyQueryError, match="agent_events"):
        run_read_only_query("select * from cache_requests", db_path=state_db, page_request=PageRequest(), user_context=editor)


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
