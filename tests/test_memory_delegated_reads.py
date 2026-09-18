"""Creation -> durable dispatch -> Memory read contracts, with test-owned state."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from pathlib import Path
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


pytestmark = pytest.mark.usefixtures("delegated_owner_transport")


@pytest.fixture
def delegated_owner_transport(monkeypatch, tmp_path):
    """Real client serialization + ASGI endpoint + verifier, no admitted flag."""
    app = create_app(_memory_controller())

    class InternalTransport(httpx.BaseTransport):
        def handle_request(self, request):
            async def dispatch():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.request(request.method, str(request.url),
                                                    headers=request.headers, content=request.read())
                    return httpx.Response(response.status_code, headers=response.headers, content=response.content)
            return asyncio.run(dispatch())

    monkeypatch.setattr("vibe.internal_client._verified_socket_path", lambda _path: tmp_path / "test.sock")
    monkeypatch.setattr("vibe.internal_client.httpx.HTTPTransport", lambda **_kwargs: InternalTransport())


@pytest.fixture
def memory_owner_turn(managers, monkeypatch):
    """Host-owned accepted Delivery for backend proof lifecycle tests."""
    from tests.test_session_delivery_fsm import _seed_session

    manager, _fresh, engine, _other, _starts = managers
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    sessions = {"ses_fsm"}

    def create(session_id="ses_wb", owner="remote:user-1", *, terminal=True):
        if session_id not in sessions:
            _seed_session(engine, session_id)
            sessions.add(session_id)

        async def accept(_session, ctx, _text, **_kwargs):
            token = ctx.platform_specific["turn_token"]
            manager._active_identity = lambda _backend, _session, logical: (logical, f"native-{logical}")
            manager.on_native_start(ctx, backend="codex", runtime_key=f"runtime-{token}", runtime_turn_id=token)

        manager._run = accept
        result = asyncio.run(manager.deliver(
            DeliveryRequest(session_id=session_id, priority="p3", content="fixture", author_id=owner),
            context=_context(session_id),
        ))
        if terminal:
            manager._terminalize_durable_turn(result.turn_id, "completed", settled_by="terminal_result",
                evidence_kind="fixture", resume_successors=False)
        return result.turn_id

    return create


def _publish_caller_env(monkeypatch, context):
    from core.caller_context import caller_env_for_platform_payload

    env = caller_env_for_platform_payload(context.platform_specific, message=context)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


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


async def _persist_and_hydrate_scheduled(manager, controller, context):
    """Use shared scheduled ingress and hydrate its actual durable snapshot."""
    from core.session_turns import capture_scheduled_provenance

    receipt = await manager.deliver(DeliveryRequest(session_id="ses_fsm", priority="p3", content="fixture",
        source="harness", author="harness", metadata={
            **context.platform_specific["message_metadata"],
            "scheduled_provenance": capture_scheduled_provenance(context),
        }), context=_context())
    with manager._sqlite_engine().connect() as conn:
        row = message_deliveries.get_delivery(conn, receipt.delivery_id)
    manager._hydrate_delivery_context(context, row)
    manager._restore_scheduled_dispatch_context(context, row)
    with manager._sqlite_engine().begin() as conn:
        assert message_deliveries.retire_queued(conn, "ses_fsm", row["id"])
    return context


def _create_definition(kind, path, *, owner_metadata=None, session_id="ses_fsm"):
    metadata = owner_metadata or {"created_by": {"caller": {"session_id": session_id, "user_id": "FORGED"}}}
    if kind == "scheduled":
        store = ScheduledTaskStore(path)
        definition = store.add_task(
            session_key=f"avibe::channel::{session_id}",
            session_id=session_id,
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
        session_key=f"avibe::channel::{session_id}",
        session_id=session_id,
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
        _publish_caller_env(monkeypatch, context)
        assert configure_memory_cli_access(direct, context)
        observed.append(await _search(direct))
        assert await _search(direct, project="notes") == observed[0]
        assert direct.memory_scope_for_cli_session("ses_fsm") == observed[0]
        definition = await asyncio.to_thread(_create_definition, kind, tmp_path / f"{kind}.json")
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
        # Restart drops the ephemeral process key; persisted owner facts alone
        # restore read scope, and a new shell receives a fresh Session proof.
        import secrets
        from core.caller_context import caller_env_for_platform_payload

        monkeypatch.setattr("core.caller_context._SESSION_PROOF_KEY", secrets.token_bytes(32))
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
            for key, value in caller_env_for_platform_payload(ctx.platform_specific, message=ctx).items():
                monkeypatch.setenv(key, value)
            chained = await asyncio.to_thread(_create_definition, "watch", tmp_path / "chained.json")
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
                _publish_caller_env(monkeypatch, ctx)
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
                task = await asyncio.to_thread(store.add_task, **args, metadata=metadata, user_context=resource)
                # A different subject cannot take the current Delivery's owner.
                from dataclasses import replace

                forged = await asyncio.to_thread(store.add_task, **args, metadata=metadata, user_context=replace(resource, subject="other"))
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
                await _persist_and_hydrate_scheduled(manager, controller, continued)
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


@pytest.mark.parametrize("proof_kind", ["other_session", "missing", "tampered", "valid"])
def test_im_session_override_rejected(managers, monkeypatch, tmp_path, proof_kind):
    """MEMORY-SEARCH-025: actual IM owner resists ordinary CLI locator overrides."""
    from core.caller_context import caller_context_from_env, caller_env_for_platform_payload, AVIBE_CALLER_SESSION_PROOF_ENV

    manager, _fresh, engine, _other_engine, _starts = managers
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    from tests.test_session_delivery_fsm import _seed_session
    _seed_session(engine, "attacker_session")
    attacker_proofs = []

    async def attacker_active(_session, context, _text, **_kwargs):
        attacker_proofs.append(caller_env_for_platform_payload(context.platform_specific, message=context)[AVIBE_CALLER_SESSION_PROOF_ENV])

    manager._run = attacker_active
    asyncio.run(manager.deliver(DeliveryRequest(session_id="attacker_session", priority="p3", content="attacker", author_id="local"), context=_context("attacker_session")))
    observed = []

    async def victim_active(_session, context, _text, **_kwargs):
        proof = caller_env_for_platform_payload(context.platform_specific, message=context)[AVIBE_CALLER_SESSION_PROOF_ENV] if proof_kind == "valid" else attacker_proofs[0]
        monkeypatch.setenv(AVIBE_CALLER_SESSION_PROOF_ENV, {"missing": "", "tampered": "伪造"}.get(proof_kind, proof))
        # A different Agent can replace these ordinary CLI environment locators.
        monkeypatch.setenv("AVIBE_SESSION_ID", "ses_fsm")
        monkeypatch.setenv("AVIBE_CALLER_PLATFORM", "slack")
        monkeypatch.setenv("AVIBE_CALLER_USER_ID", "attacker")
        caller = caller_context_from_env()
        definition = await asyncio.to_thread(
            _create_definition, "scheduled", tmp_path / "spoofed.json",
            owner_metadata={"created_by": {"caller": caller.to_metadata()}},
        )
        observed.append(definition.metadata.get("delegated_memory_owner"))

    manager._run = victim_active
    asyncio.run(manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p3", content="victim work",
                        platform="slack", scope_id="slack::user::victim",
                        author_id="victim", message_kind="original"),
        context=_context(),
    ))
    assert observed == ([{"platform": "slack", "user_id": "victim", "is_dm": True}] if proof_kind == "valid" else [None])


def test_public_continuation_payloads_hide_owner(managers):
    """Queued Delivery and accepted Message redact both known owner locations."""
    from storage.messages_service import get_message

    manager, _fresh, engine, _other_engine, _starts = managers
    owner = {"platform": "slack", "user_id": "private-user", "is_dm": True}
    private = {"delegated_memory_owner": owner}
    metadata = {
        **private,
        "visible": "retained",
        "scheduled_provenance": {"platform_specific": {"message_metadata": {**private, "visible": "nested"}}},
    }
    observed = []

    async def accept(_session, context, _text, **_kwargs):
        with engine.connect() as conn:
            turn = message_deliveries.active_turn(conn, "ses_fsm")
            delivery = message_deliveries.delivery_for_turn(conn, turn["id"])
        raw = message_deliveries.delivery_payload(delivery)
        public = message_deliveries.public_delivery_payload(delivery)
        assert raw["metadata"]["delegated_memory_owner"] == owner
        observed.append(public["metadata"])
        token = context.platform_specific["turn_token"]
        manager._active_identity = lambda _backend, _session, logical: (logical, f"native-{logical}")
        manager.on_native_start(context, backend="codex", runtime_key=f"runtime-{token}", runtime_turn_id=token)
        with engine.connect() as conn:
            delivery = message_deliveries.delivery_for_turn(conn, turn["id"])
            accepted = get_message(conn, delivery["message_id"])
            raw_message = message_deliveries.message_for_delivery(conn, delivery)
        assert "delegated_memory_owner" in raw_message["metadata_json"]
        observed.append(accepted["metadata"])

    manager._run = accept
    asyncio.run(manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p3", content="continuation", source="harness",
                        author="system", metadata=metadata),
        context=_context(),
    ))
    expected = {"visible": "retained", "scheduled_provenance": {
        "platform_specific": {"message_metadata": {"visible": "nested"}},
    }}
    assert observed == [expected, expected]


@pytest.mark.parametrize("kind", ["scheduled", "watch"])
def test_public_definition_projection_preserves_sqlite_runtime_owner(managers, monkeypatch, tmp_path, kind):
    """MEMORY-SEARCH-026: display reads and pause/resume preserve raw owners."""
    from storage.background import SQLiteBackgroundTaskStore
    from vibe.cli import _task_payload, _watch_payload

    _manager, _fresh, engine, _other_engine, _starts = managers
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    db_path = engine.url.database
    raw = SQLiteBackgroundTaskStore(db_path=Path(db_path))
    public = SQLiteBackgroundTaskStore(db_path=Path(db_path), include_private_metadata=False)
    definition = _create_definition(kind, tmp_path / f"{kind}.json")
    owner = {"platform": "slack", "user_id": "fixture", "is_dm": True}
    resource = {"sub": "fixture", "is_remote": True}
    definition.metadata.update(delegated_memory_owner=owner, resource_user_context=resource)
    try:
        if kind == "scheduled":
            raw.upsert_scheduled_task(definition.to_dict())
            shown = public.get_scheduled_task(definition.id)
            listed = public.list_scheduled_tasks()
            assert public.set_definition_enabled(definition.id, False, definition_type=kind)
            assert public.set_definition_enabled(definition.id, True, definition_type=kind)
            fallback = _task_payload(definition)
            monkeypatch.setattr("core.scheduled_tasks.SQLiteBackgroundTaskStore", lambda: raw)
            restored = ScheduledTaskStore().get_task(definition.id)
        else:
            raw.upsert_watch(definition.to_dict())
            shown = public.get_watch(definition.id)
            listed = public.list_watches()
            assert public.set_definition_enabled(definition.id, False, definition_type=kind)
            assert public.set_definition_enabled(definition.id, True, definition_type=kind)
            fallback = _watch_payload(definition, runtime_entry=None)
            monkeypatch.setattr("core.watches.SQLiteBackgroundTaskStore", lambda: raw)
            restored = ManagedWatchStore().get_watch(definition.id)
        for item in [shown, *listed, fallback]:
            assert "delegated_memory_owner" not in item["metadata"]
            assert "resource_user_context" not in item["metadata"]
        assert restored.metadata["delegated_memory_owner"] == owner
        assert restored.metadata["resource_user_context"] == resource
    finally:
        public.close()
        raw.close()


def test_owner_bound_proof_rejects_later_owner_and_subject_override(managers, delegated_owner_transport, monkeypatch, tmp_path):
    """MEMORY-SEARCH-029: an old owner cannot harvest a later owner’s scope."""
    from config.v2_config import V2Config
    from core.caller_context import caller_context_from_env, caller_resource_user_context, caller_env_for_platform_payload
    from storage import remote_access_authorization_service as auth
    from storage.resource_access_service import ResourceUserContext, metadata_with_resource_user_context
    import json
    import time
    issued_at = int(time.time())

    manager, _fresh, engine, _other, _starts = managers
    monkeypatch.setattr('storage.db.get_cached_sqlite_engine', lambda: engine)
    monkeypatch.setattr(auth, 'get_cached_sqlite_engine', lambda: engine)
    monkeypatch.setattr('core.internal_server.get_cached_sqlite_engine', lambda: engine)
    config = V2Config.default()
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.instance_id = 'fixture-instance'
    config.remote_access.vibe_cloud.instance_kind = 'personal'
    config.remote_access.vibe_cloud.instance_secret = 'fixture-not-real'
    config.save()
    transition = auth.begin_instance_binding_transition(instance_id='fixture-instance', instance_kind='personal')
    auth.complete_instance_binding_transition(instance_id='fixture-instance', instance_kind='personal', generation=transition['generation'])
    manager.controller.config.memory = SimpleNamespace(enabled=True)
    retained = {}
    outcomes = []

    async def run():
        for user in ('alice', 'alice', 'bob'):
            async def active(_session, ctx, _text, **_kwargs):
                token = ctx.platform_specific['turn_token']
                manager._active_identity = lambda _backend, _session, logical: (logical, f'native-{logical}')
                manager.on_native_start(ctx, backend='codex', runtime_key=f'runtime-{token}', runtime_turn_id=token)
                if user == 'alice':
                    # Host creates Alice's normal authenticated execution context.
                    alice = ResourceUserContext(subject='alice', instance_role='editor',
                        instance_access_source='email', instance_id='fixture-instance',
                        instance_kind='personal', is_remote=True, claims_issued_at=issued_at)
                    ctx.user_id = 'remote:alice'
                    ctx.platform_specific['message_metadata'] = metadata_with_resource_user_context({}, alice)
                    issued = caller_env_for_platform_payload(ctx.platform_specific, message=ctx, session_stable_only=True)
                    if retained:
                        assert issued == retained  # Same owner across distinct durable Turns.
                    retained.update(issued)
                    return
                for mode in ('unchanged', 'ordinary_env_sub_override', 'rightful_owner'):
                    env = dict(retained)
                    if mode != 'unchanged':
                        # Only mutate the ordinary JSON env field. No Bob credential,
                        # signed authorization, host DB change, or new HMAC is supplied.
                        resource = json.loads(env['AVIBE_CALLER_RESOURCE_CONTEXT'])
                        resource['sub'] = 'bob'
                        env['AVIBE_CALLER_RESOURCE_CONTEXT'] = json.dumps(resource)
                    if mode == 'rightful_owner':
                        bob = ResourceUserContext(subject='bob', instance_role='editor',
                            instance_access_source='email', instance_id='fixture-instance',
                            instance_kind='personal', is_remote=True, claims_issued_at=issued_at)
                        ctx.user_id = 'remote:bob'
                        ctx.platform_specific['message_metadata'] = metadata_with_resource_user_context({}, bob)
                        env = caller_env_for_platform_payload(ctx.platform_specific, message=ctx, session_stable_only=True)
                        assert env['AVIBE_CALLER_SESSION_PROOF'] != retained['AVIBE_CALLER_SESSION_PROOF']
                    for key, value in env.items():
                        monkeypatch.setenv(key, value)
                    caller = caller_context_from_env()
                    store = ScheduledTaskStore(tmp_path / f'{mode}.json')
                    task = await asyncio.to_thread(store.add_task,
                        session_key='avibe::channel::ses_fsm', session_id='ses_fsm', prompt='recall',
                        schedule_type='at', timezone_name='UTC', run_at='2099-01-01T00:00:00+00:00',
                        metadata={'created_by': {'caller': caller.to_metadata()}},
                        user_context=caller_resource_user_context(caller))
                    controller = _memory_controller()
                    scheduled = await ScheduledTaskService(controller=controller, store=store)._build_context(
                        parse_session_key('avibe::channel::ses_fsm'), session_id='ses_fsm', execution_id='',
                        task_id=task.id, trigger_kind='scheduled', metadata=task.metadata)
                    await _persist_and_hydrate_scheduled(manager, controller, scheduled)
                    admitted = configure_memory_cli_access(controller, scheduled)
                    await _search(controller, status=200 if admitted else 403)
                    outcomes.append((mode, task.metadata.get('delegated_memory_owner'), admitted))
            manager._run = active
            result = await manager.deliver(DeliveryRequest(session_id='ses_fsm', priority='p3',
                content='fixture', author_id=f'remote:{user}', message_kind='original'), context=_context())
            manager._terminalize_durable_turn(result.turn_id, 'completed', settled_by='terminal_result',
                evidence_kind='fixture', resume_successors=False)
    asyncio.run(run())
    assert outcomes == [('unchanged', None, False), ('ordinary_env_sub_override', None, False),
        ('rightful_owner', {'platform': 'avibe', 'user_id': 'remote:bob', 'is_dm': False}, True)]


def test_exact_owner_turn_never_falls_back(memory_owner_turn):
    from core.caller_context import issue_caller_session_proof, verify_caller_session_proof

    old = memory_owner_turn(owner="remote:alice")
    memory_owner_turn(owner="remote:bob", terminal=False)
    alice = issue_caller_session_proof("ses_wb", turn_id=old)
    assert verify_caller_session_proof("ses_wb", alice, {"platform": "avibe", "user_id": "remote:alice"})
    assert not verify_caller_session_proof("ses_wb", alice, {"platform": "avibe", "user_id": "remote:bob"})
    assert issue_caller_session_proof("ses_wb", turn_id="missing") is None
    assert issue_caller_session_proof("other-session", turn_id=old) is None


@pytest.mark.parametrize("metadata", [
    {"scheduled_provenance": ["invalid"]},
    {"scheduled_provenance": {"platform_specific": "invalid"}},
    {"scheduled_provenance": {"platform_specific": {"task_trigger_kind": "watch", "message_metadata": [1]}}},
    {"scheduled_provenance": {"platform_specific": {"task_trigger_kind": "watch", "message_metadata": {"delegated_memory_owner": {"user_id": "local"}}}}},
    {"scheduled_provenance": {"platform_specific": {"task_trigger_kind": "watch", "message_metadata": {"delegated_memory_owner": {"platform": "avibe", "user_id": 7}}}}},
])
def test_malformed_persisted_owner_omits_proof(managers, monkeypatch, metadata):
    """MEMORY-SEARCH-030: optional raw identity never aborts ordinary launch."""
    from core.caller_context import caller_env_for_platform_payload

    manager, _fresh, engine, _other, _starts = managers
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    seen = []

    async def run(_session, ctx, _text, **_kwargs):
        env = caller_env_for_platform_payload(ctx.platform_specific, message=ctx)
        assert "AVIBE_CALLER_SESSION_PROOF" not in env
        assert not configure_memory_cli_access(_memory_controller(), ctx)
        seen.append(True)

    manager._run = run
    asyncio.run(manager.deliver(DeliveryRequest(session_id="ses_fsm", priority="p3", content="fixture",
        source="harness", author="harness", metadata=metadata), context=_context()))
    assert seen == [True]


@pytest.mark.parametrize("change", ["owner", "absent", "resource", "same"])
@pytest.mark.parametrize("trigger", ["scheduled", "watch"])
def test_queued_authority_remains_singular(managers, monkeypatch, change, trigger):
    """MEMORY-SEARCH-031: queue batching and acceptance retain each prompt's scope."""
    from copy import deepcopy
    from tests.test_session_delivery_fsm import _activate, _row

    manager, _fresh, engine, _other, _starts = managers
    controller = _memory_controller()
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    active, _ = asyncio.run(_activate(manager))
    authority = {"delegated_memory_owner": {"platform": "avibe", "user_id": "local", "is_dm": False}}
    second = deepcopy(authority)
    if change == "owner":
        second["delegated_memory_owner"]["user_id"] = "remote:bob"
    elif change == "absent":
        second = {}
    elif change == "resource":
        # An invalid/revoked authorization must not inherit the older local grant.
        second["resource_user_context"] = {"sub": "revoked", "vibe_instance_role": "viewer"}
    queued = []
    for text, metadata in (("Alice prompt", authority), ("next prompt", second)):
        queued.append(asyncio.run(manager.deliver(DeliveryRequest(
            session_id="ses_fsm", priority="p3", content=text, source="harness", author="harness",
            author_id="same-definition", metadata={**metadata, "scheduled_provenance": {"platform_specific": {
                "task_trigger_kind": trigger, "task_definition_id": "same-definition", "message_metadata": metadata,
            }}}), context=_context())))
    observed = []

    async def dispatched(_session, ctx, text, **_kwargs):
        token = ctx.platform_specific["turn_token"]
        manager._active_identity = lambda _backend, _session, logical: (logical, f"native-{logical}")
        # Normal native acceptance runs the storage batch-identity assertion.
        manager.on_native_start(ctx, backend="codex", runtime_key=f"runtime-{token}", runtime_turn_id=token)
        admitted = configure_memory_cli_access(controller, ctx)
        scope = await _search(controller, status=200 if admitted else 403)
        observed.append((token, text, scope))

    manager._run = dispatched
    asyncio.run(manager.terminalize_turn(active))
    assert len(observed) == 1
    first_turn, first_text, first_scope = observed[0]
    assert first_scope is not None
    if change == "same":
        assert "Alice prompt" in first_text and "next prompt" in first_text
        assert _row(engine, queued[1].delivery_id)["turn_id"] == first_turn
    else:
        assert first_text == "Alice prompt"
        assert _row(engine, queued[1].delivery_id)["turn_id"] is None
        asyncio.run(manager.terminalize_turn(first_turn))
        if change == "resource":
            assert len(observed) == 1  # Current runtime authorization rejects it before native input.
            assert _row(engine, queued[1].delivery_id)["state"] in {"retired", "cancelled"}
        else:
            assert len(observed) == 2 and observed[1][0] != first_turn
            assert observed[1][1] == "next prompt"
            assert observed[1][2] != first_scope


@pytest.mark.parametrize("preexisting", [False, True])
def test_ordinary_ui_injection_never_becomes_scheduled(managers, monkeypatch, tmp_path, preexisting):
    """MEMORY-SEARCH-032: real UI persistence -> durable dispatch rejects reserved authority."""
    import json
    from sqlalchemy import update
    from storage.models import message_deliveries as delivery_table
    from tests.test_ui_session_stream import _make_session
    from tests.ui_server_test_helpers import csrf_headers
    from vibe.ui_server import app

    manager, _fresh, _engine, _other, _starts = managers
    # UI and controller consume the same test-owned database.
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    manager._engine = engine
    manager.controller.config.memory = SimpleNamespace(enabled=True)
    _, session_id = _make_session(tmp_path)
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    monkeypatch.setattr("vibe.ui_server._web_push_user_key", lambda: "local")
    monkeypatch.setattr("vibe.ui_server.is_direct_loopback_memory_request", lambda: False)
    monkeypatch.setattr("vibe.ui_server._load_remote_access_config", lambda: None)
    owner = {"platform": "avibe", "user_id": "local", "is_dm": False}
    injected = {"delegated_memory_owner": owner, "scheduled_provenance": {"platform_specific": {
        "task_trigger_kind": "watch", "delivery_source": "harness", "turn_source": "scheduled",
        "message_metadata": {"delegated_memory_owner": owner},
    }}}
    controller = _memory_controller()
    seen = []

    async def dispatched(_session, ctx, _text, **kwargs):
        token = ctx.platform_specific["turn_token"]
        manager._active_identity = lambda _backend, _session, logical: (logical, f"native-{logical}")
        manager.on_native_start(ctx, backend="codex", runtime_key=f"runtime-{token}", runtime_turn_id=token)
        from core.caller_context import caller_env_for_platform_payload
        assert "AVIBE_CALLER_SESSION_PROOF" not in caller_env_for_platform_payload(ctx.platform_specific, message=ctx)
        assert kwargs["source"] == "human"
        assert ctx.platform_specific["delivery_source"] == "user"
        assert not ctx.platform_specific.get("task_trigger_kind")
        assert "delegated_memory_owner" not in ctx.platform_specific["message_metadata"]
        assert not configure_memory_cli_access(controller, ctx)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(controller)), base_url="http://test") as client:
            response = await client.post("/internal/memory/search", headers={CALLER_SESSION_HEADER: session_id},
                json={"query": "fixture", "policy": {"mode": "keyword"}})
        assert response.status_code == 403
        controller.memory_search_payload.assert_not_called()
        seen.append(True)

    manager._run = dispatched

    async def dispatch(payload):
        with engine.begin() as conn:
            row = message_deliveries.get_delivery(conn, payload["user_message_id"])
            snapshot = json.loads(row["snapshot_json"])
            assert "delegated_memory_owner" not in json.loads(snapshot["metadata_json"])
            assert "scheduled_provenance" not in json.loads(snapshot["metadata_json"])
            if preexisting:
                # Simulate a row written before reserved-field intake sanitation.
                snapshot["metadata_json"] = json.dumps(injected)
                conn.execute(update(delivery_table).where(delivery_table.c.id == row["id"]).values(
                    snapshot_json=json.dumps(snapshot),
                    snapshot_sha256=hashlib.sha256(json.dumps(snapshot).encode()).hexdigest()))
        result = await manager.deliver(DeliveryRequest(session_id=session_id, priority="p3",
            delivery_id=row["id"], content="LAN fixture"), context=_context(session_id))
        assert result.turn_id
        return {"status_code": 202, "body": {"ok": True, "session_id": session_id, "delivery_state": "accepted"}}

    monkeypatch.setattr("vibe.internal_client.dispatch_async", dispatch)
    client = app.test_client()
    response = client.post(f"/api/sessions/{session_id}/messages",
        json={"text": "LAN fixture", "metadata": injected}, headers=csrf_headers(client))
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["author_id"] is None
    assert seen == [True]
    engine.dispose()


def test_no_trigger_harness_retains_scheduling_without_memory(managers, monkeypatch):
    """MEMORY-SEARCH-033: execution classification does not grant Memory eligibility."""
    from core.caller_context import caller_env_for_platform_payload

    manager, _fresh, engine, _other, _starts = managers
    controller = _memory_controller()
    controller.session_turns = manager
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)
    monkeypatch.setattr("core.internal_server.get_cached_sqlite_engine", lambda: engine)
    create_app(controller)
    manager._build_context = _context
    observed = []

    async def dispatched(_session, ctx, _text, **kwargs):
        assert kwargs["source"] == "scheduled"
        assert ctx.platform_specific["delivery_source"] == "harness"
        assert ctx.platform_specific["message_metadata"]["delegated_memory_owner"]["user_id"] == "local"
        assert "AVIBE_CALLER_SESSION_PROOF" not in caller_env_for_platform_payload(ctx.platform_specific, message=ctx)
        assert not configure_memory_cli_access(controller, ctx)
        await _search(controller, status=403)
        controller.memory_search_payload.assert_not_called()
        observed.append(True)

    manager._run = dispatched
    context = _context()
    context.platform_specific["message_metadata"] = {
        "delegated_memory_owner": {"platform": "avibe", "user_id": "local", "is_dm": False}}
    asyncio.run(controller.session_turn_gate.submit_scheduled("ses_fsm", context, "fixture", delivery_intent="queue"))
    assert observed == [True]
