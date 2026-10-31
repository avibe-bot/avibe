"""Session preview's owning boundaries: real transcript/config reads, no execution, safe private output.

Core dry-tick tests already cover provider encoding and transition decisions. These tests catch
integration accidentally entering dispatch, activating supply, losing caller authority, or leaking
request data through the IPC/CLI result; those risks are outside the core's dependencies.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import event, select, update
from sqlalchemy.engine import Engine

from config import paths
from core.agent_core.agent.loop import Agent
from core.agent_core.messages import ToolCallBlock, UserMessage, text
from core.handlers import request_preview
from core.internal_server import _build_session_context, create_app
from core.vibe_agents import VibeAgentStore
from modules.agents.model_hub import ModelHubRuntimeRouter
from storage import messages_service
from storage.models import agent_sessions, messages, scope_settings
from tests.agent_core.fakes import assistant
from tests.test_internal_server import _build_controller_double
from tests.test_model_hub_l3 import _source
from tests.test_model_hub_vibey_consumer import _vibey_service
from tests.test_vibey_agent import SESSION, _Harness, _insert_session, engine, session  # noqa: F401
from vibe import cli, internal_client
from vibe.authorization import AuthorizationContext, invocation_authority


def forbidden(*args, **kwargs):
    raise AssertionError("preview entered an effectful dependency")


@pytest.fixture
def preview_host(engine, session, tmp_path, monkeypatch):
    harness = _Harness(engine, tmp_path, "avibe", [])
    request = harness.request("sensitive committed input")
    harness.controller._native_start(request.context)
    # The ordinary adapter writes the first input; all assertions begin after fixture construction.
    store = VibeAgentStore()
    agent = store.create(name="preview-agent", backend="vibey", model="menu-alias", system_prompt="private agent instructions")
    with engine.begin() as conn:
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == SESSION).values(
            agent_id=agent.id, agent_name=agent.name, model="menu-alias", workdir=str(tmp_path),
            metadata_json=json.dumps({"project_base": str(tmp_path)}),
        ))
    controller = _build_controller_double()
    controller.config = harness.controller.config
    controller.vibe_agent_store = store
    controller.resolve_vibe_agent_for_context = lambda *args, **kwargs: store.require_reference_by_id(agent.id)
    source = _source("src_preview01", "Preview fixture")
    service = _vibey_service(tmp_path / "hub", [source])
    service.store.config.agents["vibey"].models[0].supports_tools = True
    router = ModelHubRuntimeRouter(service=service, turn_gateway=SimpleNamespace())
    controller.model_hub_runtime = router
    controller.agent_service.agents = {"vibey": harness.agent}
    harness.agent.controller = controller
    app = create_app(controller)
    yield SimpleNamespace(
        harness=harness, controller=controller, app=app, request=request, router=router, service=service, store=store,
    )
    store.close()


async def commit_input(host):
    await host.harness.agent.store.consume_input(
        SESSION, host.request.context.platform_specific["delivery_id"], UserMessage((text("sensitive committed input"),)),
    )


def deny_effects(host, monkeypatch):
    for name in ("_preflight", "_resume", "_accept_native_start", "handle_message", "recover_runtime_state"):
        monkeypatch.setattr(host.harness.agent, name, forbidden)
    monkeypatch.setattr(host.harness.agent, "_providers", forbidden)
    monkeypatch.setattr(host.router, "resolve", forbidden)
    monkeypatch.setattr(host.router, "_gateway_credentials", forbidden)
    monkeypatch.setattr(host.router, "_emit_transition", forbidden)
    monkeypatch.setattr(Agent, "run", forbidden)
    monkeypatch.setattr(Agent, "resume", forbidden)
    monkeypatch.setattr("modules.agents.vibey.agent.accept_catalog", forbidden)

    def no_dml(conn, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().split()[0].upper() not in {
            "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP",
        }, "request preview wrote database state"

    event.listen(Engine, "before_cursor_execute", no_dml)
    return lambda: event.remove(Engine, "before_cursor_execute", no_dml)


async def invoke(host, cut=None, caller=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app), base_url="http://internal") as client:
        return await client.post("/internal/debug/request/preview", json={
            "session_id": SESSION, "context_seq": cut, "caller": {"kind": "local"} if caller is None else caller,
        })


async def test_real_session_preview_exports_only_committed_cut_without_any_execution(preview_host, engine, monkeypatch, tmp_path):
    host = preview_host
    await commit_input(host)
    await host.harness.agent.store.append_payload(SESSION, "agent_state", {"version": 1, "state": {"secret": "opaque state"}})
    await host.harness.agent.store.append_payload(SESSION, "replay_refusal", {
        "version": 1, "source_identity": "identity", "payloads": ["hosted"],
    })
    with engine.begin() as conn:
        # This is queued, not committed model context. Preview cannot admit it.
        queued = messages_service.append(conn, scope_id=None, session_id=SESSION, platform="avibe", author="user", text="queued secret")
    (tmp_path / "AGENTS.md").write_text("project instructions marker")
    before = deepcopy(host.router._last_supply_state)
    remove = deny_effects(host, monkeypatch)
    try:
        response = await invoke(host, 2)
    finally:
        remove()
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "ready" and result["mode"] == "current_config_reconstruction"
    assert result["auth"] == "unbound" and result["context_seq"] == 2
    assert all(secret not in response.text for secret in ("sensitive committed", "opaque state", "private agent", "queued secret"))
    body = Path(result["request_body_path"]).read_bytes()
    assert b"sensitive committed input" in body and b"private agent instructions" in body
    assert b"project instructions marker" in body and b"queued secret" not in body
    assert b"auth-unbound" not in body
    assert host.router._last_supply_state == before and host.router._last_launch == {}
    assert host.harness.agent._runtimes == {} and host.harness.provider.requests == []
    assert all(not tool.calls for tool in host.harness.tools)
    with engine.connect() as conn:
        assert conn.execute(select(messages.c.context_seq).where(messages.c.id == queued["id"])).scalar_one() is None
    directory = Path(result["artifact_dir"])
    if os.name != "nt":
        assert directory.stat().st_mode & 0o777 == 0o700
        assert all(file.stat().st_mode & 0o777 == 0o600 for file in directory.iterdir())
    assert set(directory.iterdir()) == {Path(result["manifest_path"]), Path(result["request_body_path"])}
    assert "headers" not in Path(result["manifest_path"]).read_text()


async def test_fork_prefix_and_open_call_are_not_repaired(preview_host, engine, monkeypatch):
    host = preview_host
    await commit_input(host)
    with engine.begin() as conn:
        scope = conn.execute(select(agent_sessions.c.scope_id).where(agent_sessions.c.id == SESSION)).scalar_one()
        _insert_session(conn, "ses_preview_fork", scope, {"fork_source_session_id": SESSION, "fork_source_context_seq": 1})
    await host.harness.agent.store.append_response(
        SESSION, assistant("", calls=(ToolCallBlock("missing", "echo", {}),)), final=False,
    )
    request = request_preview.build_preview_request(host.controller, _build_session_context(SESSION))
    remove = deny_effects(host, monkeypatch)
    try:
        blocked = await invoke(host)
        earlier = await host.harness.agent.preview_request(SESSION, request, context_seq=1)
        fork = await host.harness.agent.preview_request("ses_preview_fork", request)
    finally:
        remove()
    result = blocked.json()
    assert result["status"] == "not_model_ready" and result["missing_tool_results"] is True
    assert result["wire_prepared"] is False and "request_body_path" not in result
    assert earlier.status == fork.status == "ready"
    assert earlier.wire.body == fork.wire.body


async def test_context_transition_is_reported_without_writing_or_sending(preview_host, monkeypatch):
    host = preview_host
    await host.harness.agent.store.consume_input(
        SESSION, host.request.context.platform_specific["delivery_id"], UserMessage((text("long " * 100_000),)),
    )
    remove = deny_effects(host, monkeypatch)
    try:
        response = await invoke(host)
    finally:
        remove()
    result = response.json()
    assert result["status"] == "transition_required" and result["transition_required"] is True
    assert result["wire_prepared"] is False and "request_body_path" not in result
    assert len(response.content) < 2000


@pytest.mark.parametrize("field", ["model", "reasoning_effort"])
def test_preview_and_dispatch_share_explicit_null_resolution(preview_host, engine, field):
    from core.agent_session_context import session_model_settings
    host = preview_host
    host.store.update("preview-agent", reasoning_effort="high")
    with engine.begin() as conn:
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == SESSION).values(**{
            field: None, "metadata_json": json.dumps({"project_base": "/tmp", "explicit_setting_overrides": [field]}),
        }))
    context = _build_session_context(SESSION)
    built = request_preview.build_preview_request(host.controller, context)
    assert getattr(built, "vibe_agent_" + field) is None
    assert getattr(built, "vibe_agent_" + field + "_explicit") is True
    assert session_model_settings(
        context.platform_specific["agent_session_target"], fallback_model="default", fallback_reasoning_effort="high",
    )[0 if field == "model" else 1] is None


async def test_preview_uses_stored_project_base_over_current_scope(preview_host, engine, monkeypatch, tmp_path):
    host = preview_host
    await commit_input(host)
    stored_project = tmp_path / "stored-project"
    cwd = stored_project / "session"
    cwd.mkdir(parents=True)
    stored_skill = stored_project / ".agents" / "skills" / "stored-bound-skill" / "SKILL.md"
    stored_skill.parent.mkdir(parents=True)
    stored_skill.write_text("---\nname: stored-bound-skill\ndescription: Stored boundary\n---\nStored body\n")
    scope_skill = tmp_path / ".agents" / "skills" / "scope-skill" / "SKILL.md"
    scope_skill.parent.mkdir(parents=True)
    scope_skill.write_text("---\nname: scope-skill\ndescription: Current scope\n---\nScope body\n")
    with engine.begin() as conn:
        scope_id = conn.execute(select(agent_sessions.c.scope_id).where(agent_sessions.c.id == SESSION)).scalar_one()
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == SESSION).values(
            workdir=str(cwd), metadata_json=json.dumps({"project_base": str(stored_project)}),
        ))
        conn.execute(update(scope_settings).where(scope_settings.c.scope_id == scope_id).values(workdir=str(tmp_path)))
    context = _build_session_context(SESSION)
    request = request_preview.build_preview_request(host.controller, context)
    assert request.context.platform_specific["agent_run_target"]["project_base"] == str(stored_project)
    sections, _ = await host.harness.agent._avibe_sections(request, cwd=str(cwd))
    assert "stored-bound-skill" in sections
    assert "scope-skill" not in sections
    remove = deny_effects(host, monkeypatch)
    try:
        response = await invoke(host)
    finally:
        remove()
    assert response.status_code == 200
    body = Path(response.json()["request_body_path"]).read_bytes()
    assert b"stored-bound-skill" in body
    assert b"scope-skill" not in body


async def test_route_description_matches_production_without_credential_activation(preview_host, monkeypatch):
    host = preview_host
    before = host.service.store.config.to_payload()
    with monkeypatch.context() as m:
        m.setattr(host.router, "_gateway_credentials", forbidden)
        m.setattr(host.router, "_emit_transition", forbidden)
        described = await host.router.describe_vibey_request("menu-alias")
    assert host.service.store.config.to_payload() == before
    assert host.router._last_launch == host.router._last_supply_state == {}
    assert described.gateway_token == "auth-unbound"
    monkeypatch.setattr(host.router, "_gateway_credentials", AsyncMock(return_value=("http://fixture.invalid/vibey", "fixture-only-token")))
    host.router.turn_gateway.correlation = SimpleNamespace(turn_hosted_tools=lambda turn: None)
    monkeypatch.setattr(host.router, "_emit_transition", lambda *args: None)
    launched = await host.router.resolve("vibey", "menu-alias", process_scope="fixture", turn_id="fixture-turn")
    assert replace(launched, gateway_base_url=described.gateway_base_url, gateway_token="auth-unbound", gateway_request_metadata={}) == described
    host.router._gateway_credentials.assert_awaited_once()

@pytest.mark.parametrize("caller", [None, {}, {"kind": "remote"}, {"kind": "remote", "resource_user_context": {}}, {"kind": "local", "extra": True}])
async def test_ipc_missing_or_malformed_authority_cannot_inherit_owner(monkeypatch, caller):
    import core.internal_server as server
    monkeypatch.setattr(server, "_build_session_context", forbidden)
    app = create_app(_build_controller_double())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://internal") as client:
        response = await client.post("/internal/debug/request/preview", json={"session_id": SESSION, "context_seq": None, "caller": caller})
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["viewer", "editor", "member", "owner"])
async def test_ipc_revalidates_existing_remote_owner_authority(preview_host, monkeypatch, role):
    from tests.scenarios.permissions.test_cli_invocation_authority import _pair_instance, _snapshot
    import core.internal_server as server

    _pair_instance()
    host = preview_host
    await commit_input(host)
    snapshot = _snapshot(role)
    if role != "owner":
        monkeypatch.setattr(server, "_build_session_context", forbidden)
    remove = deny_effects(host, monkeypatch)
    try:
        result = await invoke(host, caller={"kind": "remote", "resource_user_context": snapshot})
    finally:
        remove()
    assert result.status_code == (200 if role == "owner" else 403)
    if role == "owner":
        # A stale pairing cannot become the controller's owner either.
        snapshot["vibe_instance_id"] = "other-instance"
        assert (await invoke(host, caller={"kind": "remote", "resource_user_context": snapshot})).status_code == 403


def test_cli_parser_and_output_cannot_print_internal_payload(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["debug", "request", "preview", "--session-id", SESSION, "--context-seq", "0"])
    assert cli._CLI_COMMAND_FLOORS[("debug", "request", "preview")] == "owner"
    async def response(*args, **kwargs):
        return {"status_code": 200, "body": {"status": "ready", "auth": "unbound", "request_body_path": "/private/body", "body": "secret ciphertext", "headers": {"Authorization": "secret"}}}
    monkeypatch.setattr(internal_client, "request_preview", response)
    assert cli.cmd_debug_request_preview(args) == 0
    output = capsys.readouterr()
    assert "ciphertext" not in output.out and "Authorization" not in output.out
    assert json.loads(output.out)["request_body_path"] == "/private/body"
    with invocation_authority(AuthorizationContext(instance_role="editor", is_remote=True)):
        assert cli.cmd_debug_request_preview(args) == 1


async def test_client_preserves_remote_owner_instead_of_daemon_authority(monkeypatch, tmp_path):
    from core.control_ipc import ControlIpcClientEndpoint
    from tests.scenarios.permissions.test_cli_invocation_authority import _pair_instance, _snapshot
    from storage.resource_access_service import resource_user_context_from_metadata

    _pair_instance()
    authority = resource_user_context_from_metadata({"resource_user_context": _snapshot("owner")})
    captured = []
    def capture(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ready"})
    monkeypatch.setattr(internal_client, "_resolve_endpoint_async", AsyncMock(return_value=ControlIpcClientEndpoint(transport="unix", socket_path=tmp_path / "socket")))
    monkeypatch.setattr(internal_client, "_async_transport", lambda endpoint: httpx.MockTransport(capture))
    with invocation_authority(authority):
        await internal_client.request_preview(SESSION)
    assert captured[0]["caller"]["kind"] == "remote"
    assert captured[0]["caller"]["resource_user_context"]["vibe_instance_role"] == "owner"
    with invocation_authority(AuthorizationContext(instance_role="member", is_remote=True)):
        with pytest.raises(PermissionError):
            await internal_client.request_preview(SESSION)
    assert len(captured) == 1


async def test_artifact_refuses_unsafe_directory_and_future_cut_is_sanitized(preview_host, tmp_path):
    host = preview_host
    await commit_input(host)
    response = await invoke(host, 100)
    assert response.status_code == 409 and response.json() == {"error": "request_preview_failed"}
    root = paths.get_state_dir() / "request-previews"
    root.parent.mkdir(parents=True, exist_ok=True)
    root.symlink_to(tmp_path, target_is_directory=True)
    response = await invoke(host)
    assert response.status_code == 409
    assert not list(tmp_path.glob("preview-*"))
