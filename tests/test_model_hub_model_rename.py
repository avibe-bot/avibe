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
        from storage.background import DefinitionWriteExpectation, definition_state_unchanged

        definition = run_definitions.c.id == "reclaimed"
        stale_metadata = c.scalar(select(run_definitions.c.metadata_json).where(definition))
        task_guard = definition_state_unchanged(
            DefinitionWriteExpectation.from_read(metadata=json.loads(stale_metadata))
        )
        assert c.scalar(select(run_definitions.c.id).where(definition, *task_guard)) == "reclaimed"
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
        assert (
            c.execute(
                run_definitions.update().where(definition, *task_guard).values(metadata_json=stale_metadata)
            ).rowcount
            == 0
        )
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


@pytest.mark.parametrize("mode", ["hub", "direct"])
@pytest.mark.asyncio
async def test_opencode_pins_follow_only_the_menu_row_they_resolved_before_rename(tmp_path, mode):
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    service, store, _ = _service(tmp_path)
    agent = store.config.agents["opencode"]
    agent.mode = mode
    old, new = "base", "new/base"
    agent.models = [ModelHubBackendModelConfig(id=mid, native_protocol="anthropic") for mid in (old, "other/base")]
    baseline = [row.to_payload() for row in agent.models]
    qualified = "provider/base"
    with engine.begin() as c:
        for name, model in (("fallback", qualified), ("exact", "other/base"), ("already", new)):
            _add_agent(c, name, model)
            c.execute(agents.update().where(agents.c.id == name).values(backend="opencode"))
        _add_session(c, "session", qualified)
        c.execute(agent_sessions.update().where(agent_sessions.c.id == "session").values(agent_backend="opencode"))
        _add_scope(c, "scope", model=qualified, routing={"agent_name": "fallback", "opencode_model": qualified})
        _add_definition(c, "task", snapshot={"agent_backend": "opencode", "model": qualified})
    await service.set_agent_models("opencode", baseline, [{**baseline[0], "id": new}, baseline[1]], renames={old: new})
    expected = new if mode == "hub" else qualified
    with engine.connect() as c:
        assert c.scalar(select(agents.c.model).where(agents.c.id == "fallback")) == expected
        assert c.scalar(select(agents.c.model).where(agents.c.id == "exact")) == "other/base"
        assert c.scalar(select(agents.c.model).where(agents.c.id == "already")) == new
        assert c.scalar(select(agent_sessions.c.model).where(agent_sessions.c.id == "session")) == expected
        scope = c.execute(
            select(scope_settings.c.model, scope_settings.c.settings_json).where(scope_settings.c.scope_id == "scope")
        ).one()
        assert scope.model == json.loads(scope.settings_json)["routing"]["opencode_model"] == expected
        raw = c.scalar(select(run_definitions.c.metadata_json).where(run_definitions.c.id == "task"))
        assert json.loads(raw)["session_settings_snapshot"]["model"] == expected


def test_new_definition_visible_to_candidate_read_is_included():
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


def test_stale_candidate_cannot_overwrite_a_later_selection():
    from sqlalchemy import event
    from storage.model_selection_rename import SelectionChanged, rename_selections

    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as c:
        _add_agent(c, "stale-agent", "old")
        _add_session(c, "stale-session", "old")
    changed = False

    def compete(conn, cursor, sql, params, context, many):
        nonlocal changed
        if not changed and sql == "BEGIN IMMEDIATE":
            changed = True
            with engine.begin() as other:
                other.execute(
                    agent_sessions.update().where(agent_sessions.c.id == "stale-session").values(model="chosen")
                )

    event.listen(engine, "before_cursor_execute", compete)
    try:
        with pytest.raises(SelectionChanged):
            rename_selections("claude", {"old": "new"}, engine=engine)
    finally:
        event.remove(engine, "before_cursor_execute", compete)
    with engine.connect() as c:
        assert c.scalar(select(agents.c.model).where(agents.c.id == "stale-agent")) == "old"
        assert c.scalar(select(agent_sessions.c.model).where(agent_sessions.c.id == "stale-session")) == "chosen"


def test_unbound_pins_follow_effective_default_and_archived_scopes_keep_their_owner():
    from config import paths
    from core.vibe_agents import VibeAgentStore
    from storage.model_selection_rename import rename_selections

    ensure_sqlite_state()
    store = VibeAgentStore(paths.get_sqlite_state_path())
    store.create(name="default-test", backend="claude", model="old")
    store.set_default_agent_name("default-test")
    store.create(name="archive-test", backend="codex", model="old")
    with store.engine.begin() as c:
        _add_scope(c, "channel", model="old", routing={"agent_name": "archive-test", "model": "old"})
        _add_session(c, "unbound", "old")
        c.execute(agent_sessions.update().where(agent_sessions.c.id == "unbound").values(agent_backend=""))
        _add_definition(c, "unbound-task", snapshot={"agent_backend": "", "model": "old"})
    archived = store.archive("archive-test")
    assert archived is not None
    rename_selections("codex", {"old": "codex-new"}, engine=store.engine)
    rename_selections("claude", {"old": "claude-new"}, engine=store.engine)
    with store.engine.connect() as c:
        row = c.execute(
            select(scope_settings.c.model, scope_settings.c.settings_json).where(scope_settings.c.scope_id == "channel")
        ).one()
        routing = json.loads(row.settings_json)["routing"]
        assert routing["agent_name"] == archived.archived_name
        assert row.model == routing["model"] == "codex-new"
        assert c.scalar(select(agent_sessions.c.model).where(agent_sessions.c.id == "unbound")) == "claude-new"
        raw = c.scalar(select(run_definitions.c.metadata_json).where(run_definitions.c.id == "unbound-task"))
        assert json.loads(raw)["session_settings_snapshot"]["model"] == "claude-new"


@pytest.mark.asyncio
async def test_native_launch_handle_keeps_old_id_next_turn_launches_with_new_pin(tmp_path):
    from tests.test_codex_request_provenance import _launch, _registry

    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    service, store, _ = _service(tmp_path)
    agent = store.config.agents["codex"]
    agent.models = [ModelHubBackendModelConfig(id="old")]
    baseline = [row.to_payload() for row in agent.models]
    with engine.begin() as c:
        _add_session(c, "native", "old")
        c.execute(agent_sessions.update().where(agent_sessions.c.id == "native").values(agent_backend="codex"))
    registry = _registry(tmp_path)

    def launch(turn_id):
        with engine.connect() as c:
            model = c.scalar(select(agent_sessions.c.model).where(agent_sessions.c.id == "native"))
        token, _, metadata = _launch(
            registry, turn_id=turn_id, requested_model_id=model, gateway_request_model_id=model
        )
        return token, metadata, model

    def requested(handle):
        token, metadata, model = handle
        with registry.gateway_terminalizer(backend="codex", token=token, request_metadata=metadata) as request:
            selected = request.resolution_model(model)
            request.mark_downstream_canceled()
            return selected

    first = launch("native-first")
    await service.set_agent_models("codex", baseline, [{**baseline[0], "id": "new"}], renames={"old": "new"})
    assert requested(first) == "old"
    assert requested(launch("native-second")) == "new"
