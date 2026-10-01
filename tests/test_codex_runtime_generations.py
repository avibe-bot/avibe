"""RUNTIME-GEN-010..018: Codex app-server generations per working directory.

The fake app-server enforces Codex's cross-process thread writer lock: a thread
loaded in one process cannot be resumed in another until the first releases it.
"""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import modules.agents.codex.agent as codex_agent_module
from core.runtime_activation import RuntimeActivationRegistry
from core.runtime_ownership import RuntimeTargetOwnershipSnapshot, SessionRuntimeDisposition
from modules.agents.codex.agent import CodexAgent
from modules.agents.codex.session import CodexSessionManager
from modules.agents.codex.transport import CodexRPCError
from modules.agents.codex.turn_state import CodexTurnRegistry
from modules.agents.model_hub import ModelHubLaunch
from tests.codex_generation_support import init_generation_state


CWD_NAME = "项目 work"


class FakeAppServer:
    """One ``codex app-server`` process sharing a ``CODEX_HOME`` with its peers."""

    writers: dict[str, "FakeAppServer"] = {}
    started: list["FakeAppServer"] = []
    _ids = itertools.count(1)

    def __init__(self, binary, cwd, extra_args=None, runtime_args=None, runtime_env=None, model_hub_catalog=None):
        self.binary = binary
        self.cwd = cwd
        self.runtime_args = list(runtime_args or [])
        self.runtime_env = runtime_env
        self.loaded: set[str] = set()
        self.active: dict[str, str] = {}
        self.requests: list[tuple[str, dict]] = []
        self.alive = False
        self.stopped = False
        self.pid = next(self._ids) + 40000
        self._process = SimpleNamespace(returncode=None)
        self._notify = None
        self._closed = asyncio.Event()
        self.supports_turn_collaboration_mode = False
        self.has_pending_notifications = False

    def on_notification(self, callback):
        self._notify = callback

    def on_server_request(self, callback):
        self._server_request = callback

    async def start(self):
        self.alive = True
        FakeAppServer.started.append(self)

    async def stop(self):
        if not self.alive:
            return
        self.alive = False
        self.stopped = True
        self._process.returncode = 0
        for thread_id in list(self.loaded):
            self._release(thread_id)
        self._closed.set()

    async def wait_closed(self):
        await self._closed.wait()

    @property
    def is_alive(self):
        return self.alive

    @property
    def is_initialized(self):
        return self.alive

    def _release(self, thread_id):
        self.loaded.discard(thread_id)
        if FakeAppServer.writers.get(thread_id) is self:
            del FakeAppServer.writers[thread_id]

    def _load(self, thread_id):
        owner = FakeAppServer.writers.get(thread_id)
        if owner is not None and owner is not self:
            raise CodexRPCError({"code": -32600, "message": f"thread {thread_id} already has an active writer"})
        FakeAppServer.writers[thread_id] = self
        self.loaded.add(thread_id)

    async def send_request(self, method, params):
        self.requests.append((method, params))
        if not self.alive:
            raise ConnectionError("Codex app-server transport is not available")
        if method == "thread/start":
            thread_id = f"thread-{next(self._ids)}"
            self._load(thread_id)
            return {"thread": {"id": thread_id}}
        if method == "thread/resume":
            self._load(params["threadId"])
            return {"thread": {"id": params["threadId"]}}
        if method == "thread/unsubscribe":
            thread_id = params["threadId"]
            if thread_id not in self.loaded:
                return {"status": "notLoaded"}
            self._release(thread_id)
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self._notify("thread/closed", {"threadId": thread_id}))
            )
            return {"status": "unsubscribed"}
        if method == "thread/loaded/list":
            return {"data": sorted(self.loaded)}
        if method == "turn/start":
            thread_id = params["threadId"]
            assert thread_id in self.loaded, f"{thread_id} is not loaded in this process"
            turn_id = f"turn-{next(self._ids)}"
            self.active[thread_id] = turn_id
            return {"turn": {"id": turn_id}}
        if method == "turn/interrupt":
            self.active.pop(params["threadId"], None)
            return {}
        return {}

    async def complete(self, thread_id):
        turn_id = self.active.pop(thread_id)
        await self._notify(
            "turn/completed",
            {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}},
        )


class _EventHandler:
    def __init__(self, agent):
        self.agent = agent
        self.released = []

    async def handle_notification(self, method, params, request):
        if method == "turn/completed":
            self.agent._turn_registry.pop_turn(params["turn"]["id"])

    def clear_pending(self, turn_id):
        state = self.agent._turn_registry.hide_turn(turn_id)
        return state.request if state is not None else None

    def _release_stream_turn(self, context):
        self.released.append(context)

    def snapshot_generated_images(self, *_args):
        return None

    def bind_generated_image_snapshot(self, *_args):
        return None


@pytest.fixture(autouse=True)
def fake_app_servers(monkeypatch):
    FakeAppServer.writers = {}
    FakeAppServer.started = []
    monkeypatch.setattr(codex_agent_module, "CodexTransport", FakeAppServer)
    return FakeAppServer


class _Ownership:
    """Durable state with no owner, so only live turns keep a generation busy."""

    def snapshot_many(self, targets):
        return tuple(
            RuntimeTargetOwnershipSnapshot(
                backend=target.backend,
                resource_key=target.resource_key,
                activity_runtime_keys=(),
                sessions=(),
                sessionless_active_activity_ids=(),
                sessionless_fallback_run_ids=(),
                disposition=SessionRuntimeDisposition.RECLAIMABLE,
            )
            for target in targets
        )


class _Router:
    """Model Hub runtime router whose launch for each model a test chooses."""

    def __init__(self):
        self.launches = {}
        self.retired = []

    def snapshot(self):
        return SimpleNamespace(agents={"codex": SimpleNamespace(models=[])})

    async def resolve(self, backend, requested_model, *, process_scope=None, turn_id=None, config=None):
        return self.launches[requested_model]

    def retire_process_scope(self, backend, process_scope, **_kwargs):
        self.retired.append((backend, process_scope))


def _launch(model, channel="direct", token=None):
    hub = channel == "hub"
    return ModelHubLaunch(
        backend="codex",
        channel=channel,
        requested_model=model,
        target_model=model,
        runtime_model=model,
        source_id="source-1" if channel != "direct" else None,
        gateway_base_url="http://127.0.0.1:9/codex" if hub else None,
        gateway_token=token if hub else None,
    )


def _agent(tmp_path, *, router=None):
    agent = object.__new__(CodexAgent)
    init_generation_state(agent)
    agent.name = "codex"
    service = SimpleNamespace(
        mark_runtime_turn_started=Mock(),
        force_end_runtime_work=AsyncMock(),
    )
    agent.controller = SimpleNamespace(
        config=SimpleNamespace(language="en"),
        model_hub_runtime=router,
        runtime_activation=RuntimeActivationRegistry(),
        runtime_ownership=_Ownership(),
        agent_service=service,
        get_codex_overrides=Mock(return_value=(None, None, None)),
    )
    agent.codex_config = SimpleNamespace(binary="codex-generation-fake", extra_args=[])
    agent._registered_runtime = True
    agent._session_mgr = CodexSessionManager()
    agent._turn_registry = CodexTurnRegistry()
    agent._event_handler = _EventHandler(agent)
    agent._session_locks = {}
    agent._session_last_activity = {}
    agent._steer_reconciliation_targets = {}
    agent._user_stopped_turn_ids = set()
    agent._connection_probes = {}
    agent._connection_probe_turns = {}
    agent.sessions = SimpleNamespace(
        get_agent_session_id=lambda *_args: None,
        get_agent_session_runtime_marker=lambda *_args, **_kwargs: None,
        clear_agent_session_mapping=Mock(),
    )
    agent.ensure_agent_session_id = Mock()
    agent.bind_agent_session_id = Mock()
    agent._reserved_native_session_id = Mock(return_value=None)
    agent._delete_ack = AsyncMock()
    agent._remove_ack_reaction = AsyncMock()
    agent._build_thread_developer_instructions = AsyncMock(return_value=None)
    agent._refresh_thread_developer_instructions_if_needed = AsyncMock()
    agent._inject_caller_env_config = Mock(return_value=("", False))
    agent._caller_env_for_request = Mock(return_value={})
    agent._write_caller_env_script = Mock()
    agent._resolve_resume_model_provider_override = AsyncMock(return_value=None)
    agent._record_model_hub_native_failure = AsyncMock(return_value=False)
    # Resume binds the thread the previous turn created.
    threads = {}

    def remember(request, thread_id):
        threads[request.base_session_id] = thread_id
        return thread_id

    agent.bind_agent_session_id = Mock(side_effect=remember)
    agent.sessions.get_agent_session_id = lambda _key, base, _name: threads.get(base)
    workdir = tmp_path / CWD_NAME
    workdir.mkdir(exist_ok=True)
    return agent, str(workdir)


def _request(cwd, session, model="model-a"):
    return SimpleNamespace(
        context=SimpleNamespace(platform_specific={"agent_session_id": f"agent-{session}"}),
        message="继续处理 café",
        user_message="继续处理 café",
        working_path=cwd,
        base_session_id=session,
        composite_session_id=f"avibe:{session}",
        session_key=f"key-{session}",
        ack_message_id=None,
        subagent_name=None,
        subagent_model=None,
        subagent_reasoning_effort=None,
        vibe_agent_model=model,
        vibe_agent_model_explicit=True,
        vibe_agent_reasoning_effort=None,
        vibe_agent_reasoning_effort_explicit=False,
        vibe_agent_system_prompt=None,
        files=None,
        input_metadata=None,
    )


def _server_for(agent, session):
    return agent.transport_for_session(session)


async def _until(condition, *, timeout=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_runtime_gen_010_a_busy_directory_never_holds_a_turn_on_a_new_spec(tmp_path):
    """RUNTIME-GEN-010: S1's long turn keeps its process; S2 starts at once on a new one."""
    agent, cwd = _agent(tmp_path)
    await agent.handle_message(_request(cwd, "s1"))
    old = _server_for(agent, "s1")
    old_thread = agent._session_mgr.get_thread_id("s1")
    assert agent._turn_registry.get_active_turn("s1")

    await agent.renew_runtime(agent.codex_config)
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s2")), 1)

    new = _server_for(agent, "s2")
    assert new is not old
    assert agent._turn_registry.get_active_turn("s2")
    assert old.alive and old.active == {old_thread: agent._turn_registry.get_active_turn("s1")}

    await old.complete(old_thread)
    await _until(lambda: not old.alive)
    assert new.alive
    agent.controller.agent_service.force_end_runtime_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_gen_011_a_session_releases_its_thread_before_resuming_elsewhere(tmp_path):
    """RUNTIME-GEN-011: the old process unloads the thread; the next turn resumes it on the new one."""
    agent, cwd = _agent(tmp_path)
    await agent.handle_message(_request(cwd, "s1"))
    await agent.handle_message(_request(cwd, "s2"))
    old = _server_for(agent, "s1")
    assert _server_for(agent, "s2") is old
    s1_thread = agent._session_mgr.get_thread_id("s1")
    s2_thread = agent._session_mgr.get_thread_id("s2")
    await old.complete(s1_thread)

    await agent.renew_runtime(agent.codex_config)
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s1")), 1)

    new = _server_for(agent, "s1")
    assert new is not old
    assert ("thread/unsubscribe", {"threadId": s1_thread}) in old.requests
    assert ("thread/resume", s1_thread) in [(m, p.get("threadId")) for m, p in new.requests]
    assert agent._session_mgr.get_thread_id("s1") == s1_thread
    # S2's turn still runs where it started.
    assert old.alive and s2_thread in old.loaded
    assert _server_for(agent, "s2") is old


@pytest.mark.asyncio
async def test_runtime_gen_012_a_busy_session_moves_after_its_own_turn_is_interrupted(tmp_path):
    """RUNTIME-GEN-012: a new message to a busy session interrupts it and proceeds on the new spec."""
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "hub", token="token-a")
    agent, cwd = _agent(tmp_path, router=router)
    agent.prepare_model_hub_runtime = AsyncMock(
        return_value=SimpleNamespace(retain=lambda: SimpleNamespace(path=tmp_path / "catalog-a.json", close=Mock()))
    )
    await agent.handle_message(_request(cwd, "s1"))
    old = _server_for(agent, "s1")
    thread = agent._session_mgr.get_thread_id("s1")
    first_turn = agent._turn_registry.get_active_turn("s1")
    assert first_turn

    router.launches["model-a"] = _launch("model-a", "hub", token="token-b")
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s1")), 1)

    new = _server_for(agent, "s1")
    assert new is not old
    methods = [method for method, _ in old.requests]
    assert methods.index("turn/interrupt") < methods.index("thread/unsubscribe")
    assert agent._turn_registry.get_active_turn("s1") not in (None, first_turn)
    assert new.active.get(thread) == agent._turn_registry.get_active_turn("s1")
    assert len(agent._event_handler.released) == 1


@pytest.mark.asyncio
async def test_runtime_gen_013_at_the_cap_the_oldest_busy_generation_gives_way(tmp_path):
    """RUNTIME-GEN-013: a fourth spec starts at once; only the oldest generation's turn is interrupted."""
    agent, cwd = _agent(tmp_path)
    servers = []
    for session in ("s1", "s2", "s3"):
        await agent.handle_message(_request(cwd, session))
        servers.append(_server_for(agent, session))
        await agent.renew_runtime(agent.codex_config)
    assert len({id(server) for server in servers}) == 3

    await asyncio.wait_for(agent.handle_message(_request(cwd, "s4")), 1)
    await _until(lambda: not servers[0].alive)

    assert servers[1].alive and servers[2].alive
    assert _server_for(agent, "s4").alive
    agent.controller.agent_service.force_end_runtime_work.assert_awaited_once()
    assert agent.controller.agent_service.force_end_runtime_work.await_args.kwargs["base_session_ids"] == {"s1"}
    assert agent.transport_for_session("s1") is None


@pytest.mark.asyncio
async def test_runtime_gen_014_a_direct_launch_with_model_hub_keeps_the_managed_environment(tmp_path, monkeypatch):
    """RUNTIME-GEN-014: Model Hub enabled must not drop the desktop-managed environment."""
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "direct")
    agent, cwd = _agent(tmp_path, router=router)
    managed = {"PATH": "/opt/avibe-managed/bin", "HOME": str(tmp_path)}
    monkeypatch.setattr(agent, "_codex_runtime_environment", lambda: managed)

    await agent.handle_message(_request(cwd, "s1"))

    assert _server_for(agent, "s1").runtime_env == managed


@pytest.mark.asyncio
async def test_runtime_gen_015_direct_and_native_cli_launches_share_one_process(tmp_path):
    """RUNTIME-GEN-015: identical process inputs never replace the app-server."""
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "direct")
    router.launches["model-b"] = _launch("model-b", "native_cli")
    agent, cwd = _agent(tmp_path, router=router)

    await agent.handle_message(_request(cwd, "s1", model="model-a"))
    first = _server_for(agent, "s1")
    await first.complete(agent._session_mgr.get_thread_id("s1"))
    await agent.handle_message(_request(cwd, "s1", model="model-b"))

    assert _server_for(agent, "s1") is first
    assert len(FakeAppServer.started) == 1


@pytest.mark.asyncio
async def test_runtime_gen_016_hub_scope_is_revoked_only_with_the_last_hub_generation(tmp_path):
    """RUNTIME-GEN-016: retiring one Hub process keeps the credential its successor uses."""
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "hub", token="shared-token")
    agent, cwd = _agent(tmp_path, router=router)
    agent.prepare_model_hub_runtime = AsyncMock(
        return_value=SimpleNamespace(retain=lambda: SimpleNamespace(path=tmp_path / "catalog.json", close=Mock()))
    )
    await agent.handle_message(_request(cwd, "s1"))
    old = _server_for(agent, "s1")
    await agent.renew_runtime(agent.codex_config)
    await agent.handle_message(_request(cwd, "s2"))

    await old.complete(agent._session_mgr.get_thread_id("s1"))
    await _until(lambda: not old.alive)
    assert router.retired == []

    await agent.shutdown_runtime()
    assert router.retired == [("codex", cwd)]


@pytest.mark.asyncio
async def test_runtime_gen_017_late_thread_events_from_a_previous_generation_are_dropped(tmp_path):
    """RUNTIME-GEN-017: a moved session never receives thread-level events from the process it left."""
    agent, cwd = _agent(tmp_path)
    await agent.handle_message(_request(cwd, "s1"))
    await agent.handle_message(_request(cwd, "s2"))
    old = _server_for(agent, "s1")
    thread = agent._session_mgr.get_thread_id("s1")
    await old.complete(thread)
    await agent.renew_runtime(agent.codex_config)
    await agent.handle_message(_request(cwd, "s1"))
    handled = []
    agent._event_handler.handle_notification = AsyncMock(side_effect=lambda *args: handled.append(args[0]))

    await old._notify("error", {"threadId": thread, "error": {"message": "late"}})
    await _server_for(agent, "s1")._notify("error", {"threadId": thread, "error": {"message": "current"}})

    assert handled == ["error"]
    assert agent._event_handler.handle_notification.await_args.args[1]["error"]["message"] == "current"


@pytest.mark.asyncio
async def test_runtime_gen_018_fork_boundary_reads_the_source_sessions_generation(tmp_path):
    """RUNTIME-GEN-018: only the process running the source turn reports it in progress."""
    agent, cwd = _agent(tmp_path)
    await agent.handle_message(_request(cwd, "source"))
    source_server = _server_for(agent, "source")
    await agent.renew_runtime(agent.codex_config)
    await agent.handle_message(_request(cwd, "other"))
    target_server = _server_for(agent, "other")
    assert target_server is not source_server
    running = agent._turn_registry.get_active_turn("source")
    source_thread = agent._session_mgr.get_thread_id("source")

    async def turns(method, params):
        source_server.requests.append((method, params))
        return {"data": [{"id": running, "status": "inProgress"}, {"id": "turn-done", "status": "completed"}]}

    source_server.send_request = turns
    target_server.send_request = AsyncMock(
        return_value={"data": [{"id": running, "status": "interrupted"}]}
    )

    still_running, boundary = await agent._fork_source_last_completed_turn_id(
        target_server,
        {"source_session_id": "source", "source_native_session_id": source_thread},
    )

    assert (still_running, boundary) == (True, "turn-done")
    target_server.send_request.assert_not_awaited()
