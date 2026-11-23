"""A list rename moves selections, not upstream identities, and is retryable between stores."""

import json

import pytest
from sqlalchemy import select

from config.v2_config import ModelHubBackendModelConfig, ModelHubRouteConfig
from core.handlers.model_hub import ModelHubError
from storage.db import get_cached_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, agents, run_definitions, scope_settings
from storage.settings_revision import read_runtime_settings_revision
from tests.test_model_hub_api import _service, _set_claude_route_fixture
from tests.test_model_selection_addresses import _add_agent, _add_definition, _add_scope, _add_session


@pytest.mark.asyncio
async def test_retry_after_config_write_failure(tmp_path, monkeypatch):
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    service, store, _ = _service(tmp_path)
    old, new = "selected-old", "selected-new"
    hops = _set_claude_route_fixture(store, ("src_rename01",), old)
    # One source target serves two menu rows; only the renamed *menu* key moves.
    agent = store.config.agents["vibey"]
    store.config.agents["claude"].routes.clear()
    agent.models = [
        ModelHubBackendModelConfig(id=old, origin="provider", context_window=24000),
        ModelHubBackendModelConfig(id="other"),
    ]
    agent.sources.order = [hops[0].source_id]
    agent.routes = {mid: ModelHubRouteConfig(hops=hops, cache_ttl="1h") for mid in (old, "other")}
    agent.removed_model_ids = [new]
    baseline = [row.to_payload() for row in agent.models]
    desired = [{**baseline[0], "id": new}, baseline[1]]
    args = ("vibey", baseline, desired)
    with engine.begin() as c:
        _add_agent(c, "bot-v", old)
        c.execute(agents.update().where(agents.c.id == "bot-v").values(backend="vibey", enabled=0))
        _add_agent(c, "other-claude", old)
        _add_session(c, "pinned", old)
        c.execute(agent_sessions.update().where(agent_sessions.c.id == "pinned").values(agent_backend="vibey"))
        _add_scope(
            c,
            "channel",
            model=old,
            routing={"agent_name": "bot-v", "model": old, "model_override": old, "claude_model": old},
        )
        _add_definition(c, "reclaimed", snapshot={"agent_backend": "vibey", "model": old})
        revision = read_runtime_settings_revision(c)
    with monkeypatch.context() as patch:

        def fail_save(config):
            raise OSError("disk full")

        patch.setattr(store, "save", fail_save)
        with pytest.raises(OSError, match="disk full"):
            await service.set_agent_models(*args, renames={old: new})
    assert store.load().agents["vibey"].models[0].id == old
    with engine.connect() as c:
        assert c.scalar(select(agents.c.model).where(agents.c.id == "bot-v")) == new
        assert c.scalar(select(agents.c.model).where(agents.c.id == "other-claude")) == old
        assert c.scalar(select(agent_sessions.c.model).where(agent_sessions.c.id == "pinned")) == new
        row = c.execute(
            select(scope_settings.c.model, scope_settings.c.settings_json).where(scope_settings.c.scope_id == "channel")
        ).one()
        assert row.model == new
        assert json.loads(row.settings_json)["routing"] == {
            "agent_name": "bot-v",
            "model": new,
            "model_override": new,
            "claude_model": old,
        }
        meta = json.loads(c.scalar(select(run_definitions.c.metadata_json).where(run_definitions.c.id == "reclaimed")))
        assert meta["session_settings_snapshot"]["model"] == new
        moved_revision = read_runtime_settings_revision(c)
        assert moved_revision != revision
    await service.set_agent_models(*args, renames={old: new})
    agent = store.load().agents["vibey"]
    assert [r.to_payload() for r in agent.models] == desired
    assert agent.removed_model_ids == [old]
    assert set(agent.routes) == {new, "other"}
    assert agent.routes[new].hops == agent.routes["other"].hops == hops
    assert agent.routes[new].cache_ttl == "1h"
    assert agent.sources.order == [hops[0].source_id]
    assert store.load().sources[0].models[0].id == old
    with engine.connect() as c:
        assert read_runtime_settings_revision(c) == moved_revision


@pytest.mark.asyncio
async def test_automatic_rename_does_not_freeze_hops_or_require_removal_confirmation(tmp_path):
    ensure_sqlite_state()
    service, store, _ = _service(tmp_path)
    old, new = "old-auto", "empty-auto"
    _set_claude_route_fixture(store, ("src_rename01",), old)
    agent = store.config.agents["claude"]
    agent.routes.clear()
    agent.models = [ModelHubBackendModelConfig(id=old)]
    baseline = [r.to_payload() for r in agent.models]
    assert service.agent_chain("claude", old)["chain"]
    service.named_agents_override = lambda _: [("pinned", old)]
    await service.set_agent_models("claude", baseline, [{**baseline[0], "id": new}], renames={old: new})
    assert not store.config.agents["claude"].routes
    assert not service.agent_chain("claude", new)["chain"]


@pytest.mark.asyncio
@pytest.mark.parametrize("moves", [None, ["a", "c"], {"a": "b"}, {"a": "a"}, {"missing": "c"}, {"a": "c", "b": "c"}])
async def test_invalid_rename_has_no_selection_or_catalog_effect(tmp_path, moves):
    ensure_sqlite_state()
    service, store, _ = _service(tmp_path)
    agent = store.config.agents["claude"]
    agent.models = [ModelHubBackendModelConfig(id=x) for x in ("a", "b")]
    baseline = [r.to_payload() for r in agent.models]
    with get_cached_sqlite_engine().begin() as c:
        _add_agent(c, "guarded", "a")
    with pytest.raises(ModelHubError, match="backend_model_conflict"):
        await service.set_agent_models("claude", baseline, [{**baseline[0], "id": "c"}, baseline[1]], renames=moves)
    with get_cached_sqlite_engine().connect() as c:
        assert c.scalar(select(agents.c.model).where(agents.c.id == "guarded")) == "a"
    assert store.config.agents["claude"].models == agent.models


@pytest.mark.asyncio
async def test_opencode_rename_preserves_checked_menu_and_refuses_dormant_route(tmp_path):
    ensure_sqlite_state()
    service, store, _ = _service(tmp_path)
    hops = _set_claude_route_fixture(store, ("src_rename01",), "upstream")
    store.config.agents["claude"].routes.clear()
    agent = store.config.agents["opencode"]
    agent.models = [ModelHubBackendModelConfig(id="old", native_protocol="openai_responses")]
    agent.routes["dormant"] = ModelHubRouteConfig(hops=hops)
    baseline = [row.to_payload() for row in agent.models]

    async def rename(target):
        return await service.set_agent_models(
            "opencode", baseline, [{**baseline[0], "id": target}], renames={"old": target}
        )

    with pytest.raises(ModelHubError, match="backend_model_conflict"):
        await rename("dormant")
    await rename("new")
    assert store.config.agents["opencode"].menu.checked == ["new"]
    assert set(store.config.agents["opencode"].routes) == {"dormant"}


def test_concurrent_selection_insert_during_discovery_retries_the_whole_snapshot():
    from sqlalchemy import event
    from storage.model_selection_rename import rename_selections

    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    inserted = False

    def concurrent_writer(conn, cursor, sql, params, context, many):
        nonlocal inserted
        if not inserted and sql.startswith("SELECT scope_settings.scope_id"):
            inserted = True
            with engine.begin() as c:
                _add_definition(c, "late", snapshot={"agent_backend": "vibey", "model": "old"})

    event.listen(engine, "after_cursor_execute", concurrent_writer)
    try:
        rename_selections("vibey", {"old": "new"}, engine=engine)
    finally:
        event.remove(engine, "after_cursor_execute", concurrent_writer)
    assert inserted
    with engine.connect() as c:
        raw = c.scalar(select(run_definitions.c.metadata_json).where(run_definitions.c.id == "late"))
        assert json.loads(raw)["session_settings_snapshot"]["model"] == "new"


@pytest.mark.no_sqlite_template
def test_rename_indexes_upgrade_plan_and_downgrade(tmp_path):
    from contextlib import closing
    import sqlite3
    from alembic import command
    from storage import migrations

    path = tmp_path / "indexes.sqlite"
    previous, revision = "20261123_0099", "20261123_0100"
    indexes = (
        (agents, "backend", "ix_agents_backend_model"),
        (agent_sessions, "agent_backend", "ix_sessions_backend_model"),
    )
    migrations.run_migrations(path, revision=previous)
    migrations.run_migrations(path, revision=revision)
    with closing(sqlite3.connect(path)) as c:
        for table, owner, index in indexes:
            plan = c.execute(
                f"EXPLAIN QUERY PLAN UPDATE {table.name} SET model=? WHERE {owner}=? AND model=?",
                ("new", "vibey", "old"),
            ).fetchall()
            assert any(f"SEARCH {table.name} USING INDEX {index} ({owner}=? AND model=?)" in row[-1] for row in plan)
            assert [r[2] for r in c.execute(f"PRAGMA index_info({index})")] == list(
                next(i for i in table.indexes if i.name == index).columns.keys()
            )
    command.downgrade(migrations.alembic_config(path), previous)
    with closing(sqlite3.connect(path)) as c:
        assert all(not c.execute(f"PRAGMA index_info({index})").fetchall() for _, _, index in indexes)
