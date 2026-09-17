"""Creation -> durable dispatch -> Memory read contracts, with test-owned state."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from core.controller import Controller
from core.internal_server import create_app
from core.memory_cli_access import configure_memory_cli_access
from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore, parse_session_key
from core.session_turns import DeliveryRequest
from core.watches import ManagedWatchStore
from storage import message_deliveries
from tests.test_session_delivery_fsm import (
    _context,
    _fsm_schema_template,  # noqa: F401 -- imported pytest fixture
    managers,  # noqa: F401 -- imported pytest fixture
)
from vibe.memory_http_headers import CALLER_SESSION_HEADER


def _memory_controller():
    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(platform="avibe", memory=SimpleNamespace(enabled=True))
    controller.platform_settings_managers = {}
    controller.im_clients = {"avibe": SimpleNamespace(should_use_thread_for_reply=lambda: False)}
    controller.get_im_client_for_context = lambda _ctx: controller.im_clients["avibe"]
    controller._memory_scopes_by_session = {}
    controller._memory_cli_facts_by_session = {}
    controller.memory_runtime = SimpleNamespace(
        principal_for_user_key=lambda key: "u-" + hashlib.md5(key.encode()).hexdigest(),
    )
    controller.memory_search_payload = AsyncMock(return_value={"status": "ok", "items": []})
    return controller


async def _search(controller, *, project="default", status=200):
    app = create_app(controller)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/internal/memory/search",
            headers={CALLER_SESSION_HEADER: "ses_fsm"},
            json={"query": "non-sensitive fixture", "policy": {"mode": "keyword"}, "project": project},
        )
    assert response.status_code == status, response.text
    if status == 200:
        call = controller.memory_search_payload.call_args.kwargs
        assert call["project_id"] == project
        return call["cli_scope"]


def _create_definition(kind, path, *, owner_metadata=None):
    metadata = owner_metadata or {"created_by": {"caller": {"session_id": "ses_fsm", "user_id": "FORGED"}}}
    if kind == "scheduled":
        store = ScheduledTaskStore(path)
        definition = store.add_task(
            session_key="avibe::channel::ses_fsm",
            session_id="ses_fsm",
            prompt="find fixture",
            schedule_type="at",
            timezone_name="UTC",
            run_at="2099-01-01T00:00:00+00:00",
            metadata=metadata,
        )
        return ScheduledTaskStore(path).get_task(definition.id)
    store = ManagedWatchStore(path)
    definition = store.add_watch(
        name="fixture watch",
        session_key="avibe::channel::ses_fsm",
        session_id="ses_fsm",
        command=["true"],
        shell_command=None,
        prefix="find fixture",
        cwd=None,
        mode="once",
        timeout_seconds=0,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=1,
        post_to=None,
        deliver_key=None,
        metadata=metadata,
    )
    return ManagedWatchStore(path).get_watch(definition.id)


@pytest.mark.parametrize("kind", ["scheduled", "watch"])
def test_delegated_read_survives_definition_and_controller_restart(managers, monkeypatch, tmp_path, kind):
    """MEMORY-SEARCH-021/022: real admission and persisted Task/Watch, fresh dispatch."""
    manager, fresh, engine, _other_engine, _starts = managers
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    monkeypatch.setattr("core.internal_server.get_cached_sqlite_engine", lambda: engine)
    direct = _memory_controller()
    direct.session_turns = manager
    manager.controller.config.memory = SimpleNamespace(enabled=True)
    observed = []
    completed = []

    def accept(manager, context):
        token = context.platform_specific["turn_token"]
        manager._active_identity = lambda _backend, _session, logical: (logical, f"native-{logical}")
        manager.on_native_start(context, backend="codex", runtime_key=f"runtime-{token}", runtime_turn_id=token)

    definitions = []

    async def direct_start(_session, context, _text, **_kwargs):
        accept(manager, context)
        assert configure_memory_cli_access(direct, context)
        observed.append(await _search(direct))
        assert await _search(direct, project="notes") == observed[0]
        assert direct.memory_scope_for_cli_session("ses_fsm") == observed[0]
        definition = _create_definition(kind, tmp_path / f"{kind}.json")
        definitions.append(definition)
        assert definition.metadata["delegated_memory_owner"]["user_id"] == "local"
        scheduler = ScheduledTaskService(controller=direct, store=ScheduledTaskStore(tmp_path / "dispatch.json"))
        scheduled = await scheduler._build_context(
            parse_session_key("avibe::channel::ses_fsm"),
            session_id="ses_fsm",
            execution_id="",
            task_id=definition.id,
            trigger_kind=kind,
            metadata=definition.metadata,
        )
        result = await direct.session_turn_gate.submit_scheduled(
            "ses_fsm", scheduled, "find fixture", delivery_intent="queue"
        )
        assert result.queue_persisted

    manager._run = direct_start

    async def exercise():
        first = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="delegate fixture lookup",
                author_id="local",
                message_kind="original",
            ),
            context=_context(),
        )
        definition = definitions[0]
        manager._terminalize_durable_turn(
            first.turn_id, "completed", settled_by="terminal_result", evidence_kind="fixture", resume_successors=False
        )
        restarted = _memory_controller()
        fresh.controller.config.memory = SimpleNamespace(enabled=True)

        async def resumed(_session, ctx, _text, **_kwargs):
            accept(fresh, ctx)
            assert ctx.is_original_human_text is False
            from dataclasses import replace
            from avibe_memory.types import CaptureSkipped

            capture = restarted._memory_admission().decide(
                replace(
                    restarted._memory_turn_facts(ctx),
                    session_id="ses_fsm",
                    text="synthetic fixture",
                )
            )
            assert isinstance(capture, CaptureSkipped)
            assert ctx.platform_specific["author_id"] == definition.id
            assert configure_memory_cli_access(restarted, ctx)
            assert await _search(restarted) == observed[0]
            assert await _search(restarted, project="notes") == observed[0]
            assert restarted.memory_scope_for_cli_session("ses_fsm") is None
            app = create_app(restarted)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/internal/memory/remember",
                    headers={CALLER_SESSION_HEADER: "ses_fsm"},
                    json={"text": "synthetic input"},
                )
                assert response.status_code == 403
            # Task -> Watch must use the stamped owner, not the synthetic author.
            chained = _create_definition("watch", tmp_path / "chained.json")
            assert chained.metadata["delegated_memory_owner"] == definition.metadata["delegated_memory_owner"]
            completed.append(True)

        fresh._run = resumed
        await fresh.recover_durable_delivery_state(service_restart=True)
        assert restarted.memory_search_payload.call_count == 2
        assert completed == [True]

    asyncio.run(exercise())


def test_missing_and_forged_owner_stay_denied(managers, monkeypatch, tmp_path):
    """MEMORY-SEARCH-023: caller identity fields cannot manufacture a grant."""
    manager, _fresh, engine, _other_engine, _starts = managers
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    monkeypatch.setattr("core.internal_server.get_cached_sqlite_engine", lambda: engine)
    definition = _create_definition(
        "scheduled",
        tmp_path / "missing.json",
        owner_metadata={
            "created_by": {"caller": {"session_id": "ses_fsm", "user_id": "local"}},
            "delegated_memory_owner": {"platform": "avibe", "user_id": "local"},
        },
    )
    assert "delegated_memory_owner" not in definition.metadata
    controller = _memory_controller()
    context = _context()
    context.platform_specific.update(turn_source="scheduled", task_trigger_kind="scheduled", author_id=definition.id)
    assert not configure_memory_cli_access(controller, context)
    asyncio.run(_search(controller, status=403))
    controller.memory_search_payload.assert_not_called()


def test_remote_delegations_isolate_users_and_recheck_revoked_binding(managers, monkeypatch, tmp_path):
    """MEMORY-SEARCH-024: same project names retain user isolation; revocation wins."""
    from config.v2_config import V2Config
    from storage import remote_access_authorization_service as auth
    from storage.resource_access_service import ResourceUserContext

    manager, _fresh, engine, _other_engine, _starts = managers
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    monkeypatch.setattr("core.internal_server.get_cached_sqlite_engine", lambda: engine)
    monkeypatch.setattr(auth, "get_cached_sqlite_engine", lambda: engine)
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = "fixture-instance"
    config.remote_access.vibe_cloud.instance_kind = "personal"
    config.remote_access.vibe_cloud.instance_secret = "fixture-not-a-real-credential"
    config.save()
    transition = auth.begin_instance_binding_transition(instance_id="fixture-instance", instance_kind="personal")
    auth.complete_instance_binding_transition(
        instance_id="fixture-instance", instance_kind="personal", generation=transition["generation"]
    )
    results = []
    controllers = []
    manager.controller.config.memory = SimpleNamespace(enabled=True)

    async def exercise():
        for user in ("alice", "bob"):
            resource = ResourceUserContext(
                subject=user,
                instance_role="owner",
                instance_access_source="owner",
                instance_id="fixture-instance",
                instance_kind="personal",
                is_remote=True,
            )
            controller = _memory_controller()
            controllers.append(controller)

            async def creating(_session, ctx, _text, **_kwargs):
                token = ctx.platform_specific["turn_token"]
                manager._active_identity = lambda _backend, _session, logical: (logical, f"native-{logical}")
                manager.on_native_start(ctx, backend="codex", runtime_key=f"runtime-{token}", runtime_turn_id=token)
                store = ScheduledTaskStore(tmp_path / f"{user}.json")
                args = dict(
                    session_key="avibe::channel::ses_fsm",
                    session_id="ses_fsm",
                    prompt="recall",
                    schedule_type="at",
                    timezone_name="UTC",
                    run_at="2099-01-01T00:00:00+00:00",
                )
                metadata = {"created_by": {"caller": {"session_id": "ses_fsm", "user_id": "remote:other"}}}
                task = store.add_task(**args, metadata=metadata, user_context=resource)
                # A different subject cannot take the current Delivery's owner.
                from dataclasses import replace

                forged = store.add_task(**args, metadata=metadata, user_context=replace(resource, subject="other"))
                assert "delegated_memory_owner" not in forged.metadata
                scheduler = ScheduledTaskService(controller=controller, store=store)
                continued = await scheduler._build_context(
                    parse_session_key(args["session_key"]),
                    session_id="ses_fsm",
                    execution_id=f"run-{user}",
                    task_id=task.id,
                    metadata=task.metadata,
                    trigger_kind="scheduled",
                )
                assert configure_memory_cli_access(controller, continued)
                scope = await _search(controller, project="notes")
                assert controller.memory_scope_for_cli_session("ses_fsm") is None
                from core.caller_context import caller_context_from_platform_payload, caller_resource_user_context

                caller = caller_context_from_platform_payload(continued.platform_specific, message=continued)
                assert caller_resource_user_context(caller)["sub"] == user
                results.append(scope)

            manager._run = creating
            first = await manager.deliver(
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p3",
                    content="delegate",
                    author_id=f"remote:{user}",
                    message_kind="original",
                ),
                context=_context(),
            )
            manager._terminalize_durable_turn(
                first.turn_id,
                "completed",
                settled_by="terminal_result",
                evidence_kind="fixture",
                resume_successors=False,
            )
        assert len(results) == 2 and results[0] != results[1]
        auth.begin_instance_binding_transition(instance_id="replacement-instance", instance_kind="personal")
        for controller in controllers:
            await _search(controller, project="notes", status=403)
            assert controller.memory_search_payload.call_count == 1

    asyncio.run(exercise())
