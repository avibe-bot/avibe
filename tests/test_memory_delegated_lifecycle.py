"""Active native continuation, binding recovery, and steering authority contracts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from tests.test_session_delivery_fsm import managers, _fsm_schema_template, _context  # noqa: F401
from tests.test_memory_delegated_reads import (
    delegated_owner_transport,
    _memory_controller,
    _publish_caller_env,
    _create_definition,
    _search,
)  # noqa: F401
from core.session_turns import DeliveryRequest, capture_scheduled_provenance
from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore, parse_session_key
from core.memory_cli_access import configure_memory_cli_access
from core.services.agent_steering import SteerOutcome
from core.services.agent_steering import result as steer_result


@pytest.fixture
def active_delegated(managers, delegated_owner_transport, monkeypatch, tmp_path, request):
    manager, fresh, engine, _, _ = managers
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    monkeypatch.setattr("core.internal_server.get_cached_sqlite_engine", lambda: engine)
    kind = getattr(request, "param", "scheduled")
    remote = kind == "remote"
    create_once = kind == "create_once"
    kind = "scheduled" if remote or create_once else kind
    resource = None
    if remote:
        from config.v2_config import V2Config
        from storage import remote_access_authorization_service as auth
        from storage.resource_access_service import ResourceUserContext

        monkeypatch.setattr(auth, "get_cached_sqlite_engine", lambda: engine)
        config = V2Config.default()
        config.remote_access.vibe_cloud.enabled = True
        config.remote_access.vibe_cloud.instance_id = "fixture-instance"
        config.remote_access.vibe_cloud.instance_kind = "personal"
        config.remote_access.vibe_cloud.instance_secret = "fixture-not-real"
        config.save()
        transition = auth.begin_instance_binding_transition(instance_id="fixture-instance", instance_kind="personal")
        auth.complete_instance_binding_transition(
            instance_id="fixture-instance", instance_kind="personal", generation=transition["generation"]
        )
        resource = ResourceUserContext(
            subject="alice",
            instance_role="owner",
            instance_access_source="owner",
            instance_id="fixture-instance",
            instance_kind="personal",
            is_remote=True,
        )
    owner = "remote:alice" if remote else "local"
    controller = _memory_controller()
    manager.controller.config.memory = SimpleNamespace(enabled=True)
    manager.controller._memory_admission = controller._memory_admission
    manager.controller._memory_scopes_by_session = controller._memory_scopes_by_session
    definitions = []

    def context(sid="ses_fsm"):
        ctx = _context(sid)
        ctx.platform_specific["agent_session_target"]["agent_backend"] = "opencode"
        return ctx

    manager._build_context = context

    def accept(ctx):
        token = ctx.platform_specific["turn_token"]
        manager._active_identity = lambda _b, _s, logical: (logical, "opencode:oc-1:1")
        manager.on_native_start(ctx, backend="opencode", runtime_key="oc-1", runtime_turn_id="1")

    async def direct(_s, ctx, _t, **kw):
        accept(ctx)
        _publish_caller_env(monkeypatch, ctx)
        if resource:
            task = await asyncio.to_thread(
                ScheduledTaskStore(tmp_path / "task.json").add_task,
                session_key="avibe::channel::ses_fsm",
                session_id="ses_fsm",
                prompt="recall",
                schedule_type="at",
                timezone_name="UTC",
                run_at="2099-01-01T00:00:00+00:00",
                metadata={"created_by": {"caller": {"session_id": "ses_fsm"}}},
                user_context=resource,
            )
        else:
            task = await asyncio.to_thread(_create_definition, kind, tmp_path / "task.json")
        if create_once:
            store = ScheduledTaskStore(tmp_path / "task.json")
            task = await asyncio.to_thread(
                store.update_task,
                task.id,
                name=task.name,
                session_key=task.session_key,
                session_id=task.session_id,
                prompt=task.prompt,
                schedule_type=task.schedule_type,
                post_to=task.post_to,
                deliver_key=task.deliver_key,
                cron=task.cron,
                run_at=task.run_at,
                timezone_name=task.timezone,
                session_policy="create_once",
                metadata=task.metadata,
            )
        assert task.metadata["delegated_memory_owner"]["user_id"] == owner
        definitions.append(task)

    manager._run = direct
    from storage.resource_access_service import metadata_with_resource_user_context

    first = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="delegate",
                author_id=owner,
                metadata=metadata_with_resource_user_context({}, resource),
            ),
            context=context(),
        )
    )
    manager._terminalize_durable_turn(
        first.turn_id, "completed", settled_by="terminal_result", evidence_kind="fixture", resume_successors=False
    )

    async def task_start(_s, ctx, _t, **kw):
        accept(ctx)
        assert configure_memory_cli_access(controller, ctx)

    manager._run = task_start

    async def start():
        task = definitions[0]
        ctx = await ScheduledTaskService(
            controller=controller, store=ScheduledTaskStore(tmp_path / "task.json")
        )._build_context(
            parse_session_key("avibe::channel::ses_fsm"),
            session_id="ses_fsm",
            execution_id="",
            task_id=task.id,
            trigger_kind=kind,
            metadata=task.metadata,
        )
        return await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="Alice delegated prompt",
                source="harness",
                author="harness",
                author_id=task.id,
                metadata={**task.metadata, "scheduled_provenance": capture_scheduled_provenance(ctx)},
            ),
            context=ctx,
        )

    active = asyncio.run(start())
    assert active.turn_id
    asyncio.run(_search(controller))
    return manager, fresh, engine, controller, definitions[0], active, tmp_path


@pytest.mark.parametrize("mode", ["direct", "promote", "pending"])
@pytest.mark.parametrize("user", ["local", "remote:bob", None])
def test_steering_preserves_effective_memory_authority(active_delegated, user, mode):
    """MEMORY-SEARCH-036: native steering never mixes delegated authorities."""
    manager, _, engine, controller, task, active, tmp = active_delegated
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    scope = asyncio.run(_search(controller))
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1" if mode == "direct" else "p3",
                content="incoming request",
                author_id=user,
                message_kind="original",
            ),
            context=_context(),
        )
    )
    from storage import message_deliveries

    if mode == "promote":
        asyncio.run(manager.send_now("ses_fsm"))
    elif mode == "pending":
        with engine.begin() as conn:
            message_deliveries.open_pending_steer_batch(
                conn,
                deliveries=[message_deliveries.get_delivery(conn, result.delivery_id)],
                turn_id=active.turn_id,
                attempt_id=message_deliveries.new_attempt_id(),
            )
        asyncio.run(manager._run_pending_steers("ses_fsm", active.turn_id, _context()))
    with engine.connect() as conn:
        state = message_deliveries.get_delivery(conn, result.delivery_id)["state"]
    assert state == "accepted"
    manager._steer.assert_awaited_once()
    if user == "local":
        assert asyncio.run(_search(controller)) == scope
    else:
        asyncio.run(_search(controller, status=403))
        assert "ses_fsm" not in controller._memory_scopes_by_session


@pytest.mark.parametrize("active_delegated", ["create_once"], indirect=True)
def test_internal_rebind_preserves_owner(active_delegated, monkeypatch):
    """MEMORY-SEARCH-035: host rebind preserves identity, CLI edit re-admits."""
    manager, _, engine, controller, task, active, tmp = active_delegated
    store = ScheduledTaskStore(tmp / "task.json")
    task = store.get_task(task.id)
    assert task.metadata["delegated_memory_owner"]["user_id"] == "local"
    monkeypatch.delenv("AVIBE_CALLER_SESSION_PROOF", raising=False)
    service = ScheduledTaskService(controller=controller, store=store)
    assert task.session_policy == "create_once"
    assert service._persist_task_session_id(task, "replacement-session")
    restored = ScheduledTaskStore(tmp / "task.json").get_task(task.id)
    assert restored.metadata["delegated_memory_owner"] == task.metadata["delegated_memory_owner"]
    assert restored.session_id == "replacement-session"
    from tests.test_session_delivery_fsm import _seed_session
    from core.internal_server import create_app
    from vibe.memory_http_headers import CALLER_SESSION_HEADER
    import httpx

    _seed_session(engine, "replacement-session")
    old_scope = asyncio.run(_search(controller))
    reads = []

    async def rebound_dispatch(_session, ctx, _text, **_kw):
        assert configure_memory_cli_access(controller, ctx)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(controller)), base_url="http://test"
        ) as client:
            response = await client.post(
                "/internal/memory/search",
                headers={CALLER_SESSION_HEADER: "replacement-session"},
                json={"query": "fixture", "policy": {"mode": "keyword"}},
            )
        assert response.status_code == 200
        reads.append(controller.memory_search_payload.call_args.kwargs["cli_scope"])

    manager._run = rebound_dispatch

    async def dispatch_rebound():
        ctx = await service._build_context(
            parse_session_key(restored.session_key),
            session_id=restored.session_id,
            execution_id="",
            task_id=restored.id,
            trigger_kind="scheduled",
            metadata=restored.metadata,
        )
        await manager.deliver(
            DeliveryRequest(
                session_id=restored.session_id,
                priority="p3",
                content="rebound prompt",
                source="harness",
                author="harness",
                author_id=restored.id,
                metadata={**restored.metadata, "scheduled_provenance": capture_scheduled_provenance(ctx)},
            ),
            context=ctx,
        )

    asyncio.run(dispatch_rebound())
    assert reads == [old_scope]
    # Ordinary caller editing without its proof still loses delegation.
    service.store.update_task(
        restored.id,
        name=restored.name,
        session_key=restored.session_key,
        session_id=restored.session_id,
        prompt=restored.prompt,
        schedule_type=restored.schedule_type,
        post_to=restored.post_to,
        deliver_key=restored.deliver_key,
        cron=restored.cron,
        run_at=restored.run_at,
        timezone_name=restored.timezone,
        metadata=restored.metadata,
    )
    assert "delegated_memory_owner" not in service.store.get_task(restored.id).metadata


@pytest.mark.parametrize(
    "active_delegated, denial",
    [
        ("scheduled", None),
        ("watch", None),
        ("scheduled", "missing"),
        ("scheduled", "mismatched"),
        ("scheduled", "terminal"),
        ("remote", "revoked"),
    ],
    indirect=["active_delegated"],
)
def test_active_poll_restores_proof_and_read_scope(active_delegated, monkeypatch, denial):
    """MEMORY-SEARCH-034: revive exact live native work through current admission."""
    from tests.test_opencode_restore_polls import _make_poll, _build_agent
    from core.caller_context import verify_caller_session_proof

    manager, fresh, engine, old, task, active, tmp = active_delegated
    poll = _make_poll(platform="avibe", base_session_id="ses_fsm", opencode_session_id="oc-1")
    poll.processing_indicator = {
        "platform": "avibe",
        "user_id": task.id,
        "opencode_native_steering": {"target_session_id": "ses_fsm", "logical_turn_id": active.turn_id},
        "opencode_caller_context_env": {"AVIBE_SESSION_ID": "ses_fsm", "AVIBE_CALLER_PLATFORM": "avibe"},
    }
    if denial == "missing":
        poll.processing_indicator["opencode_native_steering"]["logical_turn_id"] = "missing"
    elif denial == "mismatched":
        poll.processing_indicator["opencode_native_steering"]["target_session_id"] = "other-session"
        poll.processing_indicator["opencode_caller_context_env"]["AVIBE_SESSION_ID"] = "other-session"
    elif denial == "terminal":
        manager._terminalize_durable_turn(
            active.turn_id, "completed", settled_by="terminal_result", evidence_kind="fixture", resume_successors=False
        )
    elif denial == "revoked":
        from storage import remote_access_authorization_service as auth

        auth.begin_instance_binding_transition(instance_id="replacement-instance", instance_kind="personal")
    agent, *_ = _build_agent({"oc-1": poll})
    new = _memory_controller()
    new.processing_indicator = agent.controller.processing_indicator
    new.session_turns = agent.controller.session_turns
    new.session_turns.controller = new
    new.session_turns._engine = engine
    new.session_turns._build_context = _context
    new.set_agent_status = lambda *args: None
    new.agent_service = agent.controller.agent_service
    agent.controller = new
    bound = []
    monkeypatch.setattr(
        "modules.agents.opencode.agent.bind_caller_context_session", lambda *args, **kw: bound.append(kw) or True
    )
    monkeypatch.setattr("modules.agents.opencode.agent.unbind_caller_context_session", lambda *args, **kw: True)

    async def run():
        release = asyncio.Event()

        async def active_poll(_poll):
            await release.wait()
            return True

        agent._poll_loop.run_restored_poll_loop = active_poll
        assert await agent.restore_active_polls() == 1
        await _search(new, status=403 if denial else 200)
        if not denial:
            new.session_turns._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
            result = await new.session_turns.deliver(
                DeliveryRequest(
                    session_id="ses_fsm", priority="p1", content="different user's follow-up", author_id="remote:bob"
                ),
                context=_context(),
            )
            assert result.state == "accepted"
            new.session_turns._steer.assert_awaited_once()
            await _search(new, status=403)
        release.set()
        await asyncio.gather(*agent._active_requests.values())

    asyncio.run(run())
    if denial:
        assert "AVIBE_CALLER_SESSION_PROOF" not in bound[0]["extra_env"]
        new.memory_search_payload.assert_not_called()
    else:
        assert verify_caller_session_proof(
            "ses_fsm", bound[0]["extra_env"]["AVIBE_CALLER_SESSION_PROOF"], {"platform": "avibe", "user_id": "local"}
        )
        assert new.memory_search_payload.await_count == 1


@pytest.mark.parametrize("active_delegated", ["remote"], indirect=True)
def test_same_remote_owner_refresh_remains_steerable(active_delegated):
    """A credential refresh preserves the same owner's resource authority."""
    from dataclasses import replace
    from storage.resource_access_service import metadata_with_resource_user_context, resource_user_context_from_metadata

    manager, _, _, controller, task, active, _ = active_delegated
    resource = resource_user_context_from_metadata(task.metadata)
    refreshed = replace(resource, claims_issued_at=(resource.claims_issued_at or 0) + 1)
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    before = asyncio.run(_search(controller))
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content="same owner's follow-up",
                author_id="remote:alice",
                metadata=metadata_with_resource_user_context({}, refreshed),
            ),
            context=_context(),
        )
    )
    assert result.state == "accepted"
    manager._steer.assert_awaited_once()
    assert asyncio.run(_search(controller)) == before


def test_memory_ineligible_group_does_not_change_steering_policy(managers):
    """Denied group Memory is not an active delegated authority to fence."""
    manager, _, _, _, _ = managers
    controller = _memory_controller()
    manager.controller.config.memory = SimpleNamespace(enabled=True)
    manager.controller._memory_admission = controller._memory_admission
    owner = {"platform": "slack", "user_id": "U1", "is_dm": False}
    metadata = {"delegated_memory_owner": owner}
    starts = []

    async def start(_s, ctx, _text, **_kw):
        token = ctx.platform_specific["turn_token"]
        manager._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
        manager.on_native_start(ctx, backend="codex", runtime_key="fixture", runtime_turn_id=token)
        assert not configure_memory_cli_access(controller, ctx)
        starts.append(token)

    manager._run = start
    asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="group task",
                source="harness",
                author="harness",
                metadata={
                    **metadata,
                    "scheduled_provenance": {
                        "platform_specific": {"task_trigger_kind": "scheduled", "message_metadata": metadata}
                    },
                },
            ),
            context=_context(),
        )
    )
    assert starts
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm", priority="p1", content="group follow-up", platform="slack", author_id="U2"
            ),
            context=_context(),
        )
    )
    assert result.state == "accepted"
    manager._steer.assert_awaited_once()
    asyncio.run(_search(controller, status=403))
    controller.memory_search_payload.assert_not_called()
