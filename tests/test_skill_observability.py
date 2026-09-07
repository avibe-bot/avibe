"""Skill statistics are optional, attributable facts, not inferred task success."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from core import skill_observability as runtime
from core.managed_skills import ManagedSkill
from storage import agent_events_retention, skill_observability as store
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_events, agent_sessions, scopes, skill_usage_daily


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


@pytest.fixture
def engine():
    ensure_sqlite_state()
    result = create_sqlite_engine()
    yield result
    result.dispose()


def skill(name="docs", *, body="Read the docs.\n", source=1):
    return ManagedSkill(name, "Project documentation", Path("/fixture/project/skills") / name, (source, 0), body)


def load(*, instant=NOW, session_id=None, item=None, error=None):
    result = runtime.observation(
        "skill.load_result",
        runtime.load_result("docs", item or skill(), duration_ms=12, error_code=error),
        session_id=session_id,
    )
    result["metadata"]["observed_at"] = store.timestamp(instant)
    return result


def write(engine, event, **kwargs):
    with engine.begin() as conn:
        return store.record(conn, event, enabled=lambda: True, now=NOW, **kwargs)


def counts(engine):
    with engine.connect() as conn:
        return (
            conn.execute(select(func.count()).select_from(agent_events)).scalar_one(),
            conn.execute(select(func.sum(skill_usage_daily.c.load_success_count))).scalar_one(),
        )


def seed_session(engine, session_id="ses-test"):
    instant = store.timestamp(NOW)
    with engine.begin() as conn:
        conn.execute(
            scopes.insert().values(
                id="scope-test",
                platform="web",
                scope_type="channel",
                native_id="native-scope",
                is_private=1,
                supports_threads=1,
                metadata_json="{}",
                first_seen_at=instant,
                last_seen_at=instant,
                updated_at=instant,
            )
        )
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id="scope-test",
                agent_name="codex",
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor",
                native_session_id="native",
                status="active",
                metadata_json="{}",
                model="mutable-current-model",
                created_at=instant,
                updated_at=instant,
            )
        )
    return session_id


def test_idempotence_and_real_repeats(engine):
    event = load()
    assert write(engine, event) == "accepted"
    assert write(engine, event) == "duplicate"
    assert write(engine, load()) == "accepted"
    assert counts(engine) == (2, 2)
    with engine.connect() as conn:
        row = conn.execute(select(skill_usage_daily)).mappings().one()
        assert row["session_id"] is None
        assert row["load_duration_samples"] == 2
        assert row["load_duration_ms_sum"] == 24
        assert row["load_duration_ms_max"] == 12
        assert row["loaded_body_bytes_sum"] == 2 * len(skill().body.encode())
    conflicting = deepcopy(event)
    conflicting["content"]["duration_ms"] = 99
    with pytest.raises(store.ObservationError, match="observation_id_conflict"):
        write(engine, conflicting)
    assert counts(engine) == (2, 2)


def test_concurrent_duplicate_receipts_and_null_grain(engine):
    event = load()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: write(engine, event), range(12)))
    assert results.count("accepted") == 1
    assert results.count("duplicate") == 11
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: write(engine, load()), range(12)))
    assert counts(engine) == (13, 13)


def test_event_and_projection_rollback_together(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER reject_skill_bucket BEFORE INSERT ON skill_usage_daily "
                "BEGIN SELECT RAISE(ABORT, 'projection rejected'); END"
            )
        )
    with pytest.raises(IntegrityError, match="projection rejected"):
        write(engine, load())
    assert counts(engine) == (0, None)


def test_session_only_never_guesses_model_or_turn(engine):
    session_id = seed_session(engine)
    event = load(session_id=session_id)
    event["turn_id"] = "old-shell-turn"
    event["metadata"]["model"] = "old-shell-model"
    event["metadata"]["backend"] = "claude"
    event["metadata"]["trigger_kind"] = "task"
    write(engine, event)
    with engine.connect() as conn:
        row = conn.execute(select(agent_events)).mappings().one()
        meta = json.loads(row["metadata_json"])
        assert (row["scope_id"], row["backend"], row["platform"]) == ("scope-test", "claude", "web")
        assert row["turn_id"] is None and row["run_id"] is None
        assert meta["model"] is None and meta["trigger_kind"] == "unknown"
        assert meta["correlation_level"] == "session_only"
    with pytest.raises(store.ObservationError, match="session_missing"):
        write(engine, load(session_id="missing-session"))


def test_clear_is_narrow_and_rejects_late_receipts(engine):
    event = load()
    write(engine, event)
    with engine.begin() as conn:
        row = dict(conn.execute(select(agent_events)).mappings().one())
        conn.execute(agent_events.insert().values(**{**row, "id": "keep-tool", "event_type": "tool_call"}))
        conn.execute(agent_events.insert().values(**{**row, "id": "keep-user", "visibility": "user"}))
        assert store.clear(conn, now=NOW) == {"events": 1, "daily_rows": 1}
    with pytest.raises(store.ObservationError, match="observation_cleared"):
        write(engine, event)
    with engine.begin() as conn:
        assert (
            store.record(
                conn, load(instant=NOW + timedelta(seconds=1)), enabled=lambda: True, now=NOW + timedelta(seconds=1)
            )
            == "accepted"
        )
        assert store.status(conn)["cleared_through"] == store.timestamp(NOW)
    assert counts(engine) == (3, 1)


def test_disabled_writer_creates_nothing(engine):
    with engine.begin() as conn:
        assert store.record(conn, load(), enabled=lambda: False, now=NOW) == "disabled"
    assert counts(engine) == (0, None)


def test_retention_boundaries_and_unrelated_rows(engine):
    ages = (366, 365, 364, 91, 90, 89, 0)
    for days in ages:
        instant = NOW - timedelta(days=days)
        with engine.begin() as conn:
            store.record(conn, load(instant=instant), enabled=lambda: True, now=instant)
    with engine.begin() as conn:
        row = dict(conn.execute(select(agent_events).limit(1)).mappings().one())
        conn.execute(agent_events.insert().values(**{**row, "id": "old-tool", "event_type": "tool_call"}))
        conn.execute(agent_events.insert().values(**{**row, "id": "old-visible", "visibility": "user"}))
    result = agent_events_retention.run_skill_retention(engine, now=NOW)
    assert result == {"events": 4, "daily_rows": 2}
    assert counts(engine) == (5, 5)
    with engine.connect() as conn:
        assert conn.execute(select(agent_events.c.id).where(agent_events.c.id == "old-tool")).scalar_one()


def test_physical_session_purge_removes_both_surfaces(engine):
    from storage.sessions_service import _delete_agent_session_rows

    session_id = seed_session(engine)
    write(engine, load(session_id=session_id))
    with engine.begin() as conn:
        row = dict(conn.execute(select(agent_events)).mappings().one())
        conn.execute(agent_events.insert().values(**{**row, "id": "keep-tool", "event_type": "tool_call"}))
        assert (
            _delete_agent_session_rows(
                conn,
                select(agent_sessions.c.id).where(agent_sessions.c.id == session_id),
                reclaim_mode="pause",
                reclaim_reason=None,
            )
            == 1
        )
        assert conn.execute(select(agent_sessions.c.id)).first() is None
        assert conn.execute(select(skill_usage_daily.c.id)).first() is None
        assert conn.execute(select(agent_events.c.id, agent_events.c.session_id)).all() == [("keep-tool", None)]
    with pytest.raises(store.ObservationError, match="session_missing"):
        write(engine, load(session_id=session_id))


def test_scope_delete_cascades_statistics(engine):
    session_id = seed_session(engine)
    write(engine, load(session_id=session_id))
    with engine.begin() as conn:
        conn.execute(scopes.delete().where(scopes.c.id == "scope-test"))
    assert counts(engine) == (0, None)


def test_archiving_retained_session_keeps_statistics(engine):
    from storage import messages_service
    from storage.sessions_service import _delete_agent_session_rows

    session_id = seed_session(engine)
    write(engine, load(session_id=session_id))
    with engine.begin() as conn:
        messages_service.append(
            conn,
            session_id=session_id,
            scope_id="scope-test",
            platform="web",
            author="user",
            message_type="user",
            text="Keep this conversation",
        )
        _delete_agent_session_rows(conn, select(agent_sessions.c.id), reclaim_mode="pause", reclaim_reason=None)
        assert conn.execute(select(agent_sessions.c.status)).scalar_one() == "archived"
    assert counts(engine) == (1, 1)


def test_schema_models_match_real_migration(engine, tmp_path):
    import re
    from sqlalchemy import inspect
    from storage.models import metadata

    declared = create_sqlite_engine(tmp_path / "declared.sqlite")
    try:
        metadata.create_all(declared)
        with engine.connect() as migrated, declared.connect() as model:

            def snapshot(conn):
                columns = conn.exec_driver_sql("PRAGMA table_info(skill_usage_daily)").all()
                # SQLite's INTEGER PRIMARY KEY is implicitly non-null either way.
                columns = [tuple(row) if row[1] != "id" else (*row[:3], 1, *row[4:]) for row in columns]
                checks = {
                    item["name"]: re.sub(r"\s+", "", item["sqltext"]).lower()
                    for item in inspect(conn).get_check_constraints("skill_usage_daily")
                }
                indexes = {
                    row[1]: (row[2], row[4]) for row in conn.exec_driver_sql("PRAGMA index_list(skill_usage_daily)")
                }
                foreign_keys = sorted(
                    tuple(row[2:]) for row in conn.exec_driver_sql("PRAGMA foreign_key_list(skill_usage_daily)")
                )
                return columns, checks, indexes, foreign_keys

            assert snapshot(migrated) == snapshot(model)
    finally:
        declared.dispose()


def test_whole_window_unique_sessions_not_daily_sum(engine):
    session_id = seed_session(engine)
    write(engine, load(session_id=session_id))
    write(engine, load(session_id=session_id, instant=NOW - timedelta(days=1)))
    with engine.connect() as conn:
        assert conn.execute(select(func.count(func.distinct(skill_usage_daily.c.session_id)))).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(skill_usage_daily)).scalar_one() == 2


def test_revision_identity_and_privacy():
    original = skill(body="Chinese: \u4e2d\u6587\n")
    body_changed = replace(original, body="Changed\n")
    description_changed = replace(original, description="Changed description")
    a = runtime.load_result("docs", original, duration_ms=0)
    b = runtime.load_result("docs", body_changed, duration_ms=0)
    c = runtime.load_result("docs", description_changed, duration_ms=0)
    assert a["skill_key"] == b["skill_key"] == c["skill_key"]
    assert a["descriptor_sha256"] == b["descriptor_sha256"] != c["descriptor_sha256"]
    assert len({a["skill_revision"], b["skill_revision"], c["skill_revision"]}) == 3
    assert a["body_bytes"] == len(original.body.encode("utf-8"))
    assert "/fixture" not in json.dumps(a) and "Chinese" not in json.dumps(a)
    builtin = replace(original, priority=(0,), directory=Path("/snapshot/a/docs"))
    moved = replace(builtin, directory=Path("/snapshot/b/docs"))
    assert runtime.skill_descriptor(builtin) == runtime.skill_descriptor(moved)


def test_catalog_counts_actual_page_and_failures_only_load_attempts(engine):
    items = [skill(f"skill-{i}") for i in range(26)]
    content = runtime.catalog_result(items, page=2)
    assert len(content["entries"]) == 1
    assert content["entries"][0]["skill_name"] == "skill-9"
    event = runtime.observation("skill.catalog_result", content)
    event["metadata"]["observed_at"] = store.timestamp(NOW)
    write(engine, event)
    failure = load(error="output_interrupted")
    write(engine, failure)
    with engine.connect() as conn:
        totals = conn.execute(
            select(
                func.sum(skill_usage_daily.c.catalog_offer_count),
                func.sum(skill_usage_daily.c.load_success_count),
                func.sum(skill_usage_daily.c.load_failure_count),
            )
        ).one()
        assert totals == (1, 0, 1)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["content"].update(body="private prompt"),
        lambda e: e["content"].update(skill_name="../private/path"),
        lambda e: e["content"].update(duration_ms=True),
        lambda e: e["metadata"].update(schema_version=2),
        lambda e: e["metadata"].update(observed_at=store.timestamp(NOW + timedelta(seconds=1))),
        lambda e: e["metadata"].update(observed_at=store.timestamp(NOW - timedelta(days=2))),
    ],
)
def test_invalid_observations_do_not_write(engine, mutate):
    event = load()
    mutate(event)
    with pytest.raises(store.ObservationError):
        write(engine, event)
    assert counts(engine) == (0, None)


def test_owner_only_sql_access_including_cte(engine):
    from storage.read_only_query import ReadOnlyQueryError, run_read_only_query
    from vibe.authorization import AuthorizationContext

    write(engine, load())
    remote = AuthorizationContext(is_remote=True, instance_role="editor")
    for table in ("skill_usage_daily", "agent_events"):
        with pytest.raises(ReadOnlyQueryError):
            run_read_only_query(
                f"WITH stats AS (SELECT * FROM {table}) SELECT count(*) FROM stats",
                page_request=None,
                user_context=remote,
            )
    assert run_read_only_query("SELECT count(*) AS n FROM skill_usage_daily", page_request=None).rows == [{"n": 1}]


def test_cli_transport_failure_preserves_output_and_exit(monkeypatch, capsys):
    from vibe import cli
    from core import managed_skills

    monkeypatch.setattr(managed_skills, "resolve_skills", lambda: [skill()])
    monkeypatch.setattr(managed_skills, "load_skill", lambda *args, **kwargs: skill())
    monkeypatch.setattr("vibe.internal_client.record_skill_observation_sync", Mock(side_effect=TimeoutError))
    assert cli.cmd_skill(SimpleNamespace(skill_command="list", page=1)) == 0
    assert capsys.readouterr().out == "- docs: Project documentation\n"
    assert cli.cmd_skill(SimpleNamespace(skill_command="load", name="docs")) == 0
    output = capsys.readouterr()
    assert "Read the docs." in output.out and output.err == ""


def test_cli_observes_actual_loaded_snapshot(monkeypatch):
    from vibe import cli

    winner = replace(skill(), body=None)
    loaded = replace(skill(), body="New body\n")
    monkeypatch.setattr("core.managed_skills.resolve_skills", lambda: [winner])
    monkeypatch.setattr("core.managed_skills.load_skill", lambda *args, **kwargs: loaded)
    submitted = Mock(return_value={"status": "queued"})
    monkeypatch.setattr("vibe.internal_client.record_skill_observation_sync", submitted)
    assert cli.cmd_skill(SimpleNamespace(skill_command="load", name="docs")) == 0
    event = submitted.call_args.args[0]
    assert event["content"]["skill_revision"] == runtime.load_result("docs", loaded, duration_ms=0)["skill_revision"]


def test_runtime_candidate_requires_acceptance(monkeypatch):
    from core.system_prompt_injection import build_system_prompt_injection

    sink = []
    recorder = Mock()
    controller = SimpleNamespace(skill_observability=recorder)
    context = SimpleNamespace(platform_specific={"agent_session_id": "ses-test", "turn_token": "turn-test"})
    monkeypatch.setattr("core.managed_skills.resolve_skills", lambda *args, **kwargs: [skill()])
    prompt = build_system_prompt_injection(skills_cwd="/fixture", skill_catalog_sink=sink)
    assert "docs" in prompt and len(sink) == 1
    recorder.enqueue.assert_not_called()
    runtime.accept_catalog(controller, context, sink[0])
    event = recorder.enqueue.call_args.args[0]
    assert event["session_id"] == "ses-test" and event["turn_id"] == "turn-test"
    assert event["metadata"]["observation_channel"] == "runtime_prompt"
    assert recorder.enqueue.call_args.kwargs == {"trusted_runtime": True}


@pytest.mark.asyncio
async def test_recorder_disabled_storage_failure_and_backpressure(engine, monkeypatch):
    recorder = runtime.SkillObservationRecorder(engine=engine, enabled=lambda: False)
    event = load(instant=store.utc_now())
    assert recorder.enqueue(event) == "queued"
    await recorder.queue.join()
    assert recorder.health()["disabled"] == 1
    recorder.enabled = lambda: True
    monkeypatch.setattr(recorder, "_write", Mock(side_effect=RuntimeError("db unavailable")))
    recorder.enqueue(event)
    await recorder.queue.join()
    assert recorder.health()["dropped"] == 1
    for _ in range(runtime.MAX_PENDING_OBSERVATIONS):
        assert recorder.enqueue(event) == "queued"
    assert recorder.enqueue(event) == "dropped"
    await recorder.close()
    assert recorder.health()["pending"] == 0
    assert recorder.enqueue(event) == "dropped"
    assert counts(engine) == (0, None)


@pytest.mark.asyncio
async def test_internal_observation_boundary(engine):
    from core.internal_server import create_app

    controller = SimpleNamespace()
    app = create_app(controller)
    recorder = controller.skill_observability
    recorder.engine = engine
    recorder.enabled = lambda: True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://internal") as client:
        event = load(instant=store.utc_now())
        assert (await client.post("/internal/skill-observations", json=event)).json() == {"status": "queued"}
        await recorder.queue.join()
        assert counts(engine) == (1, 1)
        event["metadata"]["observation_channel"] = "runtime_prompt"
        assert (await client.post("/internal/skill-observations", json=event)).status_code == 400
        assert (await client.post("/internal/skill-observations", content=b"x" * 33000)).status_code == 413
        assert (await client.get("/internal/skill-observations/health")).json()["accepted"] == 1
    await recorder.close()


def test_config_flag_is_strict_and_serialized():
    from config.v2_config import RuntimeConfig, V2Config

    assert RuntimeConfig(default_cwd=".").skill_observability_enabled is True
    assert RuntimeConfig(default_cwd=".", skill_observability_enabled=False).skill_observability_enabled is False
    for value in ("false", 0, 1, None):
        with pytest.raises(ValueError, match="skill_observability_enabled"):
            RuntimeConfig(default_cwd=".", skill_observability_enabled=value)
    config = V2Config.default()
    config.runtime.skill_observability_enabled = False
    config.save()
    assert V2Config.load().runtime.skill_observability_enabled is False


def test_collection_recovery_fails_closed(monkeypatch):
    config = SimpleNamespace(
        runtime=SimpleNamespace(skill_observability_enabled=True),
        load_warnings=["Config JSON could not be parsed; using recovery defaults"],
    )
    monkeypatch.setattr("config.v2_config.V2Config.load", lambda: config)
    assert runtime.collection_enabled() is False
    config.load_warnings = ["Recovered invalid config section 'runtime.skill_observability_enabled'"]
    assert runtime.collection_enabled() is False
    config.load_warnings = []
    assert runtime.collection_enabled() is True


def test_retention_uses_skill_partial_index(engine):
    with engine.connect() as conn:
        plan = conn.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM agent_events WHERE "
                + str(store.SKILL_TRACE_FILTER)
                + " AND datetime(created_at) IS NOT NULL AND created_at < :cutoff LIMIT 1000"
            ),
            {"cutoff": store.timestamp(NOW)},
        ).all()
    assert "ix_agent_events_skill_created_id" in str(plan)


def test_clear_command_requires_explicit_confirmation(engine, capsys):
    from vibe.cli import cmd_data_skill_usage

    write(engine, load())
    assert cmd_data_skill_usage(SimpleNamespace(clear=True, yes=False)) == 1
    assert counts(engine) == (1, 1)
    assert cmd_data_skill_usage(SimpleNamespace(clear=True, yes=True)) == 0
    assert counts(engine) == (0, None)
