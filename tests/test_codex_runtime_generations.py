"""RUNTIME-GEN-006, RUNTIME-GEN-010..018, and HFR-144: Codex app-server generations per working directory.

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
from core.native_dispatch_phase import DISPATCH_PHASE_PREWRITE, set_dispatch_phase
from core.handlers.model_hub.provenance import BoundedProvenanceStore, TurnCorrelationRegistry
from core.runtime_activation import RuntimeActivationRegistry
from core.session_activities import SessionActivityRegistry
from core.runtime_ownership import RuntimeTargetOwnershipSnapshot, SessionRuntimeDisposition
from modules.agents.codex.agent import CodexAgent
from modules.agents.codex.session import CodexSessionManager
from modules.agents.codex.transport import CodexRPCError
from modules.agents.codex.turn_state import CodexTurnRegistry
from modules.agents.model_hub import ModelHubLaunch
from modules.agents.runtime_generations import RuntimeUnitStopping
from modules.agents.service import AgentService
from tests.codex_generation_support import init_generation_state
from vibe.i18n import t as i18n_t


CWD_NAME = "项目 work"


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self._exited = asyncio.Event()

    def exit(self, code=0):
        self.returncode = code
        self._exited.set()

    async def wait(self):
        await self._exited.wait()
        return self.returncode


class FakeAppServer:
    """One ``codex app-server`` process sharing a ``CODEX_HOME`` with its peers."""

    writers: dict[str, "FakeAppServer"] = {}
    started: list["FakeAppServer"] = []
    # Awaited by every start before the server is up; it may raise.
    on_start = None
    # The next thread/resume loads its thread, then its answer is lost.
    lose_next_resume = False
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
        self._process = _FakeProcess()
        self._notify = None
        self._closed = asyncio.Event()
        self.supports_turn_collaboration_mode = False
        self.has_pending_notifications = False
        self.refuse_interrupt = False
        self.wedged = False

    def on_notification(self, callback):
        self._notify = callback

    def on_server_request(self, callback):
        self._server_request = callback

    async def start(self):
        FakeAppServer.started.append(self)
        if FakeAppServer.on_start is not None:
            await FakeAppServer.on_start(self)
        self.alive = True

    async def stop(self):
        # Like the real transport, this ends the process even after its reader closed.
        if self._process.returncode is not None:
            return
        self.alive = False
        self.stopped = True
        self._process.exit()
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
        return self.alive and not self.wedged

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
            if FakeAppServer.lose_next_resume:
                FakeAppServer.lose_next_resume = False
                self.wedged = True
                raise TimeoutError("Codex RPC thread/resume timed out after 120s")
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
            if self.refuse_interrupt:
                raise CodexRPCError({"code": -32603, "message": "interrupt refused"})
            self.active.pop(params["threadId"], None)
            return {}
        return {}

    def close_reader(self):
        """Stdout closes, but the process lives on and keeps its writer locks."""
        self.alive = False

    def exit(self):
        for thread_id in list(self.loaded):
            self._release(thread_id)
        self._process.exit(-9)

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
    FakeAppServer.on_start = None
    FakeAppServer.lose_next_resume = False
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
        self.scopes = []
        self.retired = []

    def snapshot(self):
        return SimpleNamespace(agents={"codex": SimpleNamespace(models=[])})

    async def resolve(self, backend, requested_model, *, process_scope=None, turn_id=None, config=None):
        self.scopes.append((backend, process_scope))
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
async def test_runtime_gen_006_disabling_codex_ends_every_app_server_and_admits_nothing(tmp_path, monkeypatch):
    """RUNTIME-GEN-006: disabling Codex ends every app-server at once, a busy one included.

    The core has already interrupted the backend's work with its own notice,
    so the stop shows none. A turn that captured the agent before it left
    routing fails visibly with the localized error and starts no process,
    whether its directory has an app-server or not.
    """
    failures = AsyncMock()
    monkeypatch.setattr(codex_agent_module, "emit_backend_failure", failures)
    router = _Router()
    router.launches["model-a"] = _launch("model-a")
    agent, cwd = _agent(tmp_path, router=router)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    await agent.handle_message(_request(cwd, "s1"))
    busy = _server_for(agent, "s1")
    await agent.handle_message(_request(str(elsewhere), "s2"))
    idle = _server_for(agent, "s2")
    await idle.complete(agent._session_mgr.get_thread_id("s2"))
    started = len(FakeAppServer.started)
    # One turn for a directory with no app-server yet is resolving its launch.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    resolving, resolved = asyncio.Event(), asyncio.Event()
    resolve = router.resolve
    resolutions = []

    async def slow_resolve(*args, **kwargs):
        resolutions.append(args)
        resolving.set()
        await resolved.wait()
        return await resolve(*args, **kwargs)

    router.resolve = slow_resolve
    in_flight = asyncio.create_task(agent.handle_message(_request(str(fresh), "s3")))
    await asyncio.wait_for(resolving.wait(), 1)

    await asyncio.wait_for(agent.shutdown_runtime(), 1)

    assert busy.stopped and idle.stopped
    assert not any(agent._runtimes.values())
    agent.controller.agent_service.force_end_runtime_work.assert_not_awaited()
    resolved.set()
    await asyncio.wait_for(in_flight, 1)
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s4")), 1)
    assert [type(call.kwargs["cause"]) for call in failures.await_args_list] == [RuntimeUnitStopping] * 2
    assert {call.kwargs["display_text"] for call in failures.await_args_list} == {
        f"❌ {i18n_t('error.agentRuntimeRetired', 'en', agent='Codex')}"
    }
    assert len(FakeAppServer.started) == started
    assert len(resolutions) == 1  # The late turn resolved no launch.


@pytest.mark.asyncio
async def test_runtime_gen_006_a_disable_whose_stop_fails_is_retried_until_the_app_server_is_gone(tmp_path):
    """RUNTIME-GEN-006: a disable that leaves an app-server running fails, so the idle sweep retries it.

    The agent is already out of routing, so the teardown is the only owner of
    that process; a shutdown that returned normally would lose it.
    """
    agent, cwd = _agent(tmp_path)
    await agent.handle_message(_request(cwd, "s1"))
    server = _server_for(agent, "s1")
    stop = server.stop
    server.stop = AsyncMock(side_effect=RuntimeError("the child did not stop"))
    service = AgentService(controller=SimpleNamespace())
    attempts = []

    async def disable():
        attempts.append(len(attempts) + 1)
        await agent.shutdown_runtime()

    assert not await service.run_until_done("codex", disable)
    assert server.alive

    server.stop = stop
    await service.retry_pending()
    await service.retry_pending()  # A finished teardown is not run again.

    assert server.stopped and not any(agent._runtimes.values())
    assert attempts == [1, 2]


@pytest.mark.asyncio
async def test_runtime_gen_006_a_retried_disable_settles_only_the_disabled_agents_work(tmp_path):
    """RUNTIME-GEN-006: a disable settles its own agent's work even after Codex is enabled again.

    The core's interrupt failed and the first teardown failed before settling.
    Re-enabled meanwhile, a new agent runs the same Session in the same
    directory, so its Activity has the same runtime key. The retried teardown
    settles the old agent's Activity with the disable reason and leaves the
    new agent's work alone.
    """
    activation = RuntimeActivationRegistry()
    activities = SessionActivityRegistry(activation_registry=activation)
    service = AgentService(controller=SimpleNamespace(), activities=activities, activation_registry=activation)
    end_work = service.force_end_runtime_work
    service.force_end_runtime_work = AsyncMock(side_effect=RuntimeError("settlement store unavailable"))

    def enabled_agent():
        agent, cwd = _agent(tmp_path)
        agent.controller.runtime_activation = activation
        agent.controller.agent_service = service
        return agent, cwd

    def start_activity(agent, activity_id):
        activities.start(
            backend="codex",
            runtime_key=f"s1:{cwd}",
            session_id="ses-1",
            activity_id=activity_id,
            kind="task",
            activation_identity=agent._generation_for_session("s1").runtime.activation,
        )

    disabled, cwd = enabled_agent()
    await disabled.handle_message(_request(cwd, "s1"))
    old = _server_for(disabled, "s1")
    start_activity(disabled, "old-task")
    with pytest.raises(RuntimeError, match="survived shutdown"):
        await disabled.shutdown_runtime(settle_reason="backend_disabled")
    assert old.alive

    reenabled, _ = enabled_agent()
    await reenabled.handle_message(_request(cwd, "s1"))
    new = _server_for(reenabled, "s1")
    start_activity(reenabled, "new-task")
    turns_settled = []

    async def recorded_end_work(backend, *, base_session_ids, reason, **kwargs):
        turns_settled.append((set(base_session_ids), reason))
        await end_work(backend, base_session_ids=base_session_ids, reason=reason, **kwargs)

    service.force_end_runtime_work = recorded_end_work
    settled = []
    service.on_activity_terminal = settled.append

    await disabled.shutdown_runtime(settle_reason="backend_disabled")

    assert [(item.id, item.status, item.metadata.get("interrupt_reason")) for item in settled] == [
        ("old-task", "killed", "backend_disabled")
    ]
    assert [item["id"] for item in activities.session_state("ses-1")["background_activities"]] == ["new-task"]
    # The disabled agent still knew its own running turn, so it settled it too.
    assert turns_settled == [({"s1"}, "backend_disabled")]
    assert old.stopped and new.alive
    assert reenabled._turn_registry.get_active_turn("s1")


@pytest.mark.asyncio
async def test_runtime_gen_016_a_retried_disable_never_revokes_a_reenabled_agents_hub_credential(tmp_path):
    """RUNTIME-GEN-016: a disabled agent's retried teardown retires only its own Hub scope.

    Its first teardown left a Hub app-server running, and the re-enabled
    agent started its own in the same directory. When the retry stops the old
    process, the old credential is revoked and the new process's still
    authenticates at the gateway.
    """
    correlation = TurnCorrelationRegistry(BoundedProvenanceStore(tmp_path / "provenance.json"))

    class _GatewayRouter(_Router):
        async def resolve(self, backend, requested_model, *, process_scope=None, turn_id=None, config=None):
            token = correlation.credentials(backend, process_scope, None, request_scoped=True)
            return _launch(requested_model, "hub", token=token)

        def retire_process_scope(self, backend, process_scope, **_kwargs):
            correlation.retire_scope(backend, process_scope)

    router = _GatewayRouter()
    catalog = SimpleNamespace(retain=lambda: SimpleNamespace(path=tmp_path / "catalog.json", close=Mock()))
    disabled, cwd = _agent(tmp_path, router=router)
    reenabled, _ = _agent(tmp_path, router=router)
    for agent in (disabled, reenabled):
        agent.prepare_model_hub_runtime = AsyncMock(return_value=catalog)

    await disabled.handle_message(_request(cwd, "s1"))
    old = _server_for(disabled, "s1")
    stop = old.stop
    old.stop = AsyncMock(side_effect=RuntimeError("the child did not stop"))
    with pytest.raises(RuntimeError, match="survived shutdown"):
        await disabled.shutdown_runtime(settle_reason="backend_disabled")
    await reenabled.handle_message(_request(cwd, "s2"))
    new = _server_for(reenabled, "s2")
    old.stop = stop

    await disabled.shutdown_runtime(settle_reason="backend_disabled")

    assert old.stopped and new.alive
    assert not correlation.authenticates("codex", old.runtime_env["AVIBE_MODEL_HUB_TOKEN"])
    assert correlation.authenticates("codex", new.runtime_env["AVIBE_MODEL_HUB_TOKEN"])


@pytest.mark.asyncio
async def test_runtime_gen_006_a_reenabled_agent_starts_beside_a_teardown_that_holds_its_activation(tmp_path, monkeypatch):
    """RUNTIME-GEN-006: a re-enabled agent's first generation never aliases a disabled agent's.

    Both number their first generation in a directory as serial 1. While the
    disabled agent's retried teardown holds that generation's activation
    reserved for retirement, the re-enabled agent's turn in the same
    directory still attaches its own and runs.
    """
    failures = AsyncMock()
    monkeypatch.setattr(codex_agent_module, "emit_backend_failure", failures)
    activation = RuntimeActivationRegistry()
    disabled, cwd = _agent(tmp_path)
    reenabled, _ = _agent(tmp_path)
    for agent in (disabled, reenabled):
        agent.controller.runtime_activation = activation
    await disabled.handle_message(_request(cwd, "s1"))
    old = _server_for(disabled, "s1")
    stopping, release = asyncio.Event(), asyncio.Event()
    stop = old.stop

    async def slow_stop():
        stopping.set()
        await release.wait()
        await stop()

    old.stop = slow_stop
    teardown = asyncio.create_task(disabled.shutdown_runtime(settle_reason="backend_disabled"))
    await asyncio.wait_for(stopping.wait(), 1)

    await asyncio.wait_for(reenabled.handle_message(_request(cwd, "s2")), 1)

    failures.assert_not_awaited()
    assert reenabled._turn_registry.get_active_turn("s2")
    release.set()
    await asyncio.wait_for(teardown, 1)
    assert old.stopped and _server_for(reenabled, "s2").alive


@pytest.mark.asyncio
async def test_runtime_gen_016_a_process_whose_post_start_setup_fails_is_stopped_by_the_sweep(tmp_path, monkeypatch):
    """RUNTIME-GEN-016: whatever fails after the spawn, the started app-server stays the sweep's to stop."""
    agent, cwd = _agent(tmp_path)
    failures = AsyncMock()
    monkeypatch.setattr(codex_agent_module, "emit_backend_failure", failures)
    monkeypatch.setattr(
        agent, "_attach_runtime_activation", Mock(side_effect=RuntimeError("activation unavailable"))
    )

    await asyncio.wait_for(agent.handle_message(_request(cwd, "s1")), 1)

    failures.assert_awaited_once()
    started = FakeAppServer.started[-1]
    assert started.alive
    await agent.reap_runtime_generations()
    assert started.stopped and not any(agent._runtimes.values())


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
@pytest.mark.parametrize("process_exits", [False, True], ids=["process-lives", "process-exits"])
async def test_runtime_gen_011_a_closed_reader_is_no_proof_of_release(tmp_path, monkeypatch, process_exits):
    """RUNTIME-GEN-011: only the old process's exit releases a thread its closed reader can no longer unload."""
    monkeypatch.setattr(codex_agent_module, "_THREAD_RELEASE_TIMEOUT_SECONDS", 0.2)
    failures = AsyncMock()
    monkeypatch.setattr(codex_agent_module, "emit_backend_failure", failures)
    agent, cwd = _agent(tmp_path)
    await agent.handle_message(_request(cwd, "s1"))
    old = _server_for(agent, "s1")
    thread = agent._session_mgr.get_thread_id("s1")
    await old.complete(thread)
    old_generation = agent._generation_for_session("s1")
    old.close_reader()
    if process_exits:
        asyncio.get_running_loop().call_later(0.02, old.exit)
    else:
        # Avibe ends a child whose reader closed, but here that stop fails.
        old.stop = AsyncMock(side_effect=RuntimeError("the child did not stop"))

    await agent.renew_runtime(agent.codex_config)
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s1")), 1)

    resumed = [
        server for server in FakeAppServer.started
        if server is not old and ("thread/resume", thread) in [(m, p.get("threadId")) for m, p in server.requests]
    ]
    if process_exits:
        assert resumed and _server_for(agent, "s1") is resumed[0]
        failures.assert_not_awaited()
    else:
        # The process still holds the thread: the input is held, nothing moved.
        assert resumed == []
        assert agent._generation_for_session("s1") is old_generation
        assert isinstance(failures.await_args.kwargs["cause"], codex_agent_module.CodexThreadReleaseUnavailableError)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["accepted", "refused"])
async def test_runtime_gen_012_a_busy_session_moves_after_its_own_turn_is_interrupted(tmp_path, monkeypatch, interrupt):
    """RUNTIME-GEN-012: a new message to a busy session interrupts it and proceeds on the new spec.

    A refused interrupt changes nothing: the running turn stays tracked on its
    process and the new input is held.
    """
    failures = AsyncMock()
    monkeypatch.setattr(codex_agent_module, "emit_backend_failure", failures)
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
    first_context = agent._turn_registry.get_request_for_turn(first_turn).context
    assert first_turn

    router.launches["model-a"] = _launch("model-a", "hub", token="token-b")
    old.refuse_interrupt = interrupt == "refused"
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s1")), 1)

    methods = [method for method, _ in old.requests]
    if interrupt == "refused":
        assert "thread/unsubscribe" not in methods
        assert _server_for(agent, "s1") is old
        assert agent._turn_registry.get_active_turn("s1") == first_turn
        assert old.active == {thread: first_turn}
        assert all(context is not first_context for context in agent._event_handler.released)
        assert isinstance(failures.await_args.kwargs["cause"], codex_agent_module.CodexThreadReleaseUnavailableError)
        return
    new = _server_for(agent, "s1")
    assert new is not old
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
async def test_runtime_gen_013_a_session_leaving_a_force_stopped_generation_waits_for_its_teardown(tmp_path):
    """RUNTIME-GEN-013: a turn admitted while the cap's forced stop settles work is never part of that work."""
    agent, cwd = _agent(tmp_path)
    servers = []
    for session in ("s1", "s2", "s3"):
        await agent.handle_message(_request(cwd, session))
        servers.append(_server_for(agent, session))
        await agent.renew_runtime(agent.codex_config)
    events = []
    oldest = servers[0]
    stop_oldest = oldest.stop

    async def stop():
        events.append("oldest stopped")
        await stop_oldest()

    oldest.stop = stop
    start_turn = agent._start_turn

    async def recording_start_turn(transport, request, *args, **kwargs):
        if request.base_session_id == "s1":
            events.append("s1 turn started")
        return await start_turn(transport, request, *args, **kwargs)

    agent._start_turn = recording_start_turn
    follow_up = []

    async def settle(*_args, **_kwargs):
        # S1 sends again while its generation's work is being settled.
        follow_up.append(asyncio.create_task(agent.handle_message(_request(cwd, "s1"))))
        await asyncio.sleep(0.05)
        events.append("settled")

    agent.controller.agent_service.force_end_runtime_work = AsyncMock(side_effect=settle)

    await asyncio.wait_for(agent.handle_message(_request(cwd, "s4")), 1)
    await _until(lambda: bool(follow_up))
    await asyncio.wait_for(follow_up[0], 2)

    assert events == ["settled", "oldest stopped", "s1 turn started"]
    assert agent._turn_registry.get_active_turn("s1")


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_taken", [False, True], ids=["turn-holds-its-gate", "a-later-turn-holds-the-gate"])
async def test_runtime_gen_013_a_child_whose_reader_closed_is_settled_before_it_is_killed(tmp_path, gate_taken):
    """RUNTIME-GEN-013: a process that can no longer report its turn is ended like a forced stop.

    Once the liveness monitor settled that turn and the Session's next turn took
    its gate, settling the Session again would cancel the next turn instead.
    """
    agent, cwd = _agent(tmp_path)
    first = _request(cwd, "s1")
    await agent.handle_message(first)
    old = _server_for(agent, "s1")
    old.close_reader()
    agent.controller.agent_service.emit_matches_runtime_turn = lambda context: not (
        gate_taken and context is first.context
    )

    await agent.renew_runtime(agent.codex_config)
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s2")), 1)
    await _until(lambda: old.stopped)

    agent.controller.agent_service.force_end_runtime_work.assert_awaited_once()
    settled = agent.controller.agent_service.force_end_runtime_work.await_args.kwargs["base_session_ids"]
    assert settled == (set() if gate_taken else {"s1"})


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
async def test_runtime_gen_015_a_renewal_during_hub_catalog_preparation_leaves_one_coherent_spec(tmp_path):
    """RUNTIME-GEN-015: a Hub turn's spec derives from one load of its launch inputs.

    A renewal that lands while the turn's catalog is prepared changes neither
    its binary nor its epoch midway: the turn runs on the app-server its inputs
    describe, and the directory's next turn moves to the renewed one.
    """
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "hub", token="hub-token")
    agent, cwd = _agent(tmp_path, router=router)
    catalog = SimpleNamespace(retain=lambda: SimpleNamespace(path=tmp_path / "catalog.json", close=Mock()))
    agent.prepare_model_hub_runtime = AsyncMock(return_value=catalog)
    await agent.handle_message(_request(cwd, "s1"))
    first = _server_for(agent, "s1")
    await first.complete(agent._session_mgr.get_thread_id("s1"))

    async def renewed_while_preparing(*_args, **_kwargs):
        await agent.renew_runtime(SimpleNamespace(binary="codex-renewed", extra_args=[]))
        return catalog

    agent.prepare_model_hub_runtime = AsyncMock(side_effect=renewed_while_preparing)
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s2")), 1)

    assert _server_for(agent, "s2") is first
    assert [server.binary for server in FakeAppServer.started] == ["codex-generation-fake"]

    agent.prepare_model_hub_runtime = AsyncMock(return_value=catalog)
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s3")), 1)
    assert [server.binary for server in FakeAppServer.started] == ["codex-generation-fake", "codex-renewed"]


@pytest.mark.asyncio
async def test_runtime_gen_015_a_renewal_during_hub_launch_resolution_leaves_one_coherent_spec(tmp_path):
    """RUNTIME-GEN-015: a Hub turn's launch and spec derive from the one load taken at admission.

    A renewal that lands while the turn's Model Hub launch is resolved changes
    neither its binary nor its epoch: the turn runs on the app-server its
    admission load describes, and the directory's next turn moves.
    """
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "hub", token="hub-token")
    agent, cwd = _agent(tmp_path, router=router)
    catalog = SimpleNamespace(retain=lambda: SimpleNamespace(path=tmp_path / "catalog.json", close=Mock()))
    agent.prepare_model_hub_runtime = AsyncMock(return_value=catalog)
    await agent.handle_message(_request(cwd, "s1"))
    first = _server_for(agent, "s1")
    await first.complete(agent._session_mgr.get_thread_id("s1"))
    resolve = router.resolve

    async def renewed_while_resolving(*args, **kwargs):
        await agent.renew_runtime(SimpleNamespace(binary="codex-renewed", extra_args=[]))
        return await resolve(*args, **kwargs)

    router.resolve = renewed_while_resolving
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s2")), 1)

    assert _server_for(agent, "s2") is first
    assert [server.binary for server in FakeAppServer.started] == ["codex-generation-fake"]

    router.resolve = resolve
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s3")), 1)
    assert [server.binary for server in FakeAppServer.started] == ["codex-generation-fake", "codex-renewed"]


@pytest.mark.asyncio
async def test_runtime_gen_015_a_dead_retiring_generation_is_never_reused(tmp_path):
    """RUNTIME-GEN-015: an equal spec reuses a process only while that process can serve."""
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "hub", token="shared-token")
    agent, cwd = _agent(tmp_path, router=router)
    agent.controller.emit_agent_message = AsyncMock()
    agent.prepare_model_hub_runtime = AsyncMock(
        return_value=SimpleNamespace(retain=lambda: SimpleNamespace(path=tmp_path / "catalog.json", close=Mock()))
    )
    await agent.handle_message(_request(cwd, "s1"))
    hub = _server_for(agent, "s1")
    # A switch to Direct leaves the busy Hub process retiring, then it dies.
    router.launches["model-a"] = _launch("model-a")
    await agent.handle_message(_request(cwd, "s2"))
    assert _server_for(agent, "s2") is not hub
    hub.close_reader()
    hub.exit()
    hub_requests = len(hub.requests)

    router.launches["model-a"] = _launch("model-a", "hub", token="shared-token")
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s3")), 1)

    assert hub.requests[hub_requests:] == []
    served = _server_for(agent, "s3")
    assert served not in (hub, _server_for(agent, "s2")) and served.alive
    assert agent._turn_registry.get_active_turn("s3")


@pytest.mark.asyncio
async def test_runtime_gen_015_a_generation_that_dies_while_its_spec_is_prepared_is_not_reused(tmp_path):
    """RUNTIME-GEN-015: usability is checked again where the equal-spec process is acquired."""
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "hub", token="shared-token")
    agent, cwd = _agent(tmp_path, router=router)
    agent.controller.emit_agent_message = AsyncMock()
    catalog = SimpleNamespace(retain=lambda: SimpleNamespace(path=tmp_path / "catalog.json", close=Mock()))
    agent.prepare_model_hub_runtime = AsyncMock(return_value=catalog)
    await agent.handle_message(_request(cwd, "s1"))
    hub = _server_for(agent, "s1")
    await hub.complete(agent._session_mgr.get_thread_id("s1"))
    await agent.adopt_model_hub_catalog()

    async def prepare(*_args, **_kwargs):
        # The equal-spec process dies while the next turn prepares its catalog.
        hub.close_reader()
        hub.exit()
        return catalog

    agent.prepare_model_hub_runtime = AsyncMock(side_effect=prepare)
    hub_requests = len(hub.requests)
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s2")), 1)

    assert hub.requests[hub_requests:] == []
    assert _server_for(agent, "s2") is not hub and agent._turn_registry.get_active_turn("s2")


@pytest.mark.asyncio
async def test_runtime_gen_016_a_starting_hub_process_keeps_the_scope(tmp_path):
    """RUNTIME-GEN-016: a Hub process still starting counts as the directory's Hub process."""
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "hub", token="shared-token")
    agent, cwd = _agent(tmp_path, router=router)
    agent.prepare_model_hub_runtime = AsyncMock(
        return_value=SimpleNamespace(retain=lambda: SimpleNamespace(path=tmp_path / "catalog.json", close=Mock()))
    )
    await agent.handle_message(_request(cwd, "s1"))
    retiring_hub = _server_for(agent, "s1")
    # A switch to Direct leaves the busy Hub process retiring.
    router.launches["model-a"] = _launch("model-a")
    await agent.handle_message(_request(cwd, "s2"))
    # Back to the Hub on a new spec: its process takes a while to start.
    router.launches["model-a"] = _launch("model-a", "hub", token="shared-token")
    await agent.renew_runtime(agent.codex_config)
    started = asyncio.Event()
    up = asyncio.Event()

    async def slow_start(_server):
        started.set()
        await up.wait()

    FakeAppServer.on_start = slow_start
    s3 = asyncio.create_task(agent.handle_message(_request(cwd, "s3")))
    await asyncio.wait_for(started.wait(), 1)

    await retiring_hub.complete(agent._session_mgr.get_thread_id("s1"))
    await _until(lambda: not retiring_hub.alive)
    assert router.retired == []

    up.set()
    await asyncio.wait_for(s3, 1)
    assert router.retired == []


@pytest.mark.asyncio
@pytest.mark.parametrize("child", ["exited", "alive"])
async def test_runtime_gen_016_a_failed_hub_start_is_cleaned_up(tmp_path, monkeypatch, child):
    """RUNTIME-GEN-016: a Hub start that fails still revokes the scope and never leaks its child."""
    monkeypatch.setattr(codex_agent_module, "emit_backend_failure", AsyncMock())
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "hub", token="shared-token")
    agent, cwd = _agent(tmp_path, router=router)
    agent.prepare_model_hub_runtime = AsyncMock(
        return_value=SimpleNamespace(retain=lambda: SimpleNamespace(path=tmp_path / "catalog.json", close=Mock()))
    )

    async def failing_start(server):
        if child == "exited":
            server._process.exit(1)
        raise RuntimeError("initialize failed")

    FakeAppServer.on_start = failing_start
    await agent.handle_message(_request(cwd, "s1"))
    [server] = FakeAppServer.started
    if child == "alive":
        # Its own cleanup failed; the sweep stops it.
        assert router.retired == []
        await agent.reap_runtime_generations()
        assert server.stopped

    assert router.retired == [router.scopes[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize("successor", ["hub", "direct"], ids=["hub-renewal", "switch-to-direct"])
async def test_runtime_gen_016_hub_scope_is_revoked_only_with_the_last_hub_generation(tmp_path, successor):
    """RUNTIME-GEN-016: retiring one Hub process keeps the credential its running turn or successor uses.

    A switch to Direct is a spec change: the next turn starts at once on a
    Direct process while the Hub turn finishes on its own process and keeps the
    gateway credential until it ends.
    """
    router = _Router()
    router.launches["model-a"] = _launch("model-a", "hub", token="shared-token")
    agent, cwd = _agent(tmp_path, router=router)
    agent.prepare_model_hub_runtime = AsyncMock(
        return_value=SimpleNamespace(retain=lambda: SimpleNamespace(path=tmp_path / "catalog.json", close=Mock()))
    )
    await agent.handle_message(_request(cwd, "s1"))
    old = _server_for(agent, "s1")
    if successor == "hub":
        await agent.renew_runtime(agent.codex_config)
    else:
        # The mode switch commits, then the catalog hook adopts it.
        router.launches["model-a"] = _launch("model-a")
        await agent.adopt_model_hub_catalog()
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s2")), 1)

    new = _server_for(agent, "s2")
    assert new is not old and agent._turn_registry.get_active_turn("s2")
    assert ("AVIBE_MODEL_HUB_TOKEN" in new.runtime_env) is (successor == "hub")
    assert old.alive and agent._turn_registry.get_active_turn("s1")
    assert router.retired == []

    await old.complete(agent._session_mgr.get_thread_id("s1"))
    await _until(lambda: not old.alive)
    agent.controller.agent_service.force_end_runtime_work.assert_not_awaited()
    if successor == "hub":
        assert router.retired == []
        await agent.shutdown_runtime()
    assert router.retired == [router.scopes[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled_on", ["first-attempt", "retry"])
async def test_runtime_gen_010_a_cancelled_admission_never_pins_its_generation(tmp_path, fake_app_servers, cancelled_on):
    """RUNTIME-GEN-010: a turn start cancelled before Codex answered leaves nothing that keeps its process.

    That holds on the retry too, after the first attempt failed before Codex saw it.
    """
    agent, cwd = _agent(tmp_path)
    await agent.handle_message(_request(cwd, "s1"))
    old = _server_for(agent, "s1")
    await old.complete(agent._session_mgr.get_thread_id("s1"))
    hung = []

    def hang_turn_start(server):
        send = server.send_request

        async def hanging_send(method, params):
            if method == "turn/start":
                hung.append(server)
                await asyncio.Event().wait()
            return await send(method, params)

        server.send_request = hanging_send

    if cancelled_on == "first-attempt":
        hang_turn_start(old)
    else:
        send = old.send_request

        async def broken_send(method, params):
            if method == "thread/start":
                raise ConnectionError("Codex app-server transport is not available")
            return await send(method, params)

        old.send_request = broken_send

        async def on_start(server):
            hang_turn_start(server)

        fake_app_servers.on_start = on_start
    request = _request(cwd, "s2")
    # As AgentService marks a turn before handing it over.
    set_dispatch_phase(request.context, DISPATCH_PHASE_PREWRITE)
    admission = asyncio.create_task(agent.handle_message(request))
    await _until(lambda: bool(hung))
    admission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await admission

    fake_app_servers.on_start = None
    await agent.renew_runtime(agent.codex_config)
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s3")), 1)
    await agent.reap_runtime_generations()
    assert not hung[0].alive


@pytest.mark.asyncio
async def test_runtime_gen_011_end_releases_the_thread_of_a_process_already_being_stopped(tmp_path):
    """RUNTIME-GEN-011: End proves the release of a thread whose process the reconciler took over.

    That stop may still decline, so the process can outlive End.
    """
    agent, cwd = _agent(tmp_path)
    await agent.handle_message(_request(cwd, "s1"))
    old = _server_for(agent, "s1")
    thread = agent._session_mgr.get_thread_id("s1")
    await old.complete(thread)
    checking, decide = asyncio.Event(), asyncio.Event()

    async def drained(_generation):
        checking.set()
        await decide.wait()
        return False  # Something still owns the process, so the stop declines.

    agent._generation_drained = drained
    unit = agent._units[cwd]
    await unit.retire(unit.current)
    await asyncio.wait_for(checking.wait(), 1)

    ended = await asyncio.wait_for(agent.end_session("s1"), 1)
    decide.set()
    await unit.settled()

    assert ended["process_killed"] is False
    assert old.alive and thread not in old.loaded
    assert agent._generation_for_session("s1") is None


@pytest.mark.asyncio
async def test_runtime_gen_011_new_releases_a_session_that_started_while_it_cleared(tmp_path):
    """RUNTIME-GEN-011: ``/new`` clears a whole session key, so it releases every Session it clears."""
    agent, cwd = _agent(tmp_path)
    agent.sessions.clear_agent_sessions = Mock()
    await agent.handle_message(_request(cwd, "s1"))
    server = _server_for(agent, "s1")
    await server.complete(agent._session_mgr.get_thread_id("s1"))
    joining = _request(cwd, "s2")
    joining.session_key = "key-s1"
    send = server.send_request

    async def send_while_another_session_starts(method, params):
        if method == "thread/unsubscribe" and not agent._session_mgr.get_sessions_by_session_key("key-s1")[1:]:
            # Another Session of the same key starts while the first is released.
            await agent.handle_message(joining)
        return await send(method, params)

    server.send_request = send_while_another_session_starts

    await asyncio.wait_for(agent.clear_sessions("key-s1"), 2)

    assert server.loaded == set()
    assert agent._generation_for_session("s2") is None
    assert agent._session_mgr.get_sessions_by_session_key("key-s1") == []


@pytest.mark.asyncio
async def test_runtime_gen_011_a_resume_whose_answer_was_lost_is_released_before_resuming_elsewhere(tmp_path, monkeypatch):
    """RUNTIME-GEN-011: a process that may have loaded the thread releases it before another resumes it."""
    monkeypatch.setattr(codex_agent_module, "emit_backend_failure", AsyncMock())
    agent, cwd = _agent(tmp_path)
    await agent.handle_message(_request(cwd, "s1"))
    thread = agent._session_mgr.get_thread_id("s1")
    await _server_for(agent, "s1").complete(thread)
    await agent.renew_runtime(agent.codex_config)
    # S2's running turn keeps the next process alive after it fails S1.
    await agent.handle_message(_request(cwd, "s2"))
    lost = _server_for(agent, "s2")
    FakeAppServer.lose_next_resume = True
    await asyncio.wait_for(agent.handle_message(_request(cwd, "s1")), 2)
    assert lost.alive and thread in lost.loaded

    await asyncio.wait_for(agent.handle_message(_request(cwd, "s1")), 2)

    served = _server_for(agent, "s1")
    assert served not in (None, lost)
    assert ("thread/unsubscribe", {"threadId": thread}) in lost.requests
    assert served.active.get(thread) == agent._turn_registry.get_active_turn("s1")


@pytest.mark.asyncio
async def test_a_new_thread_serves_turns_only_once_it_is_persisted(tmp_path, monkeypatch):
    """A thread the durable mapping lacks would vanish from resume history."""
    monkeypatch.setattr(codex_agent_module, "emit_backend_failure", AsyncMock())
    agent, cwd = _agent(tmp_path)
    remember = agent.bind_agent_session_id.side_effect
    writes = iter([RuntimeError("database is locked")])

    def bind(request, thread_id):
        failure = next(writes, None)
        if failure is not None:
            raise failure
        return remember(request, thread_id)

    agent.bind_agent_session_id.side_effect = bind

    await agent.handle_message(_request(cwd, "s1"))
    assert agent._turn_registry.get_active_turn("s1") is None
    await agent.handle_message(_request(cwd, "s1"))

    thread = agent._session_mgr.get_thread_id("s1")
    assert agent.sessions.get_agent_session_id("key-s1", "s1", "codex") == thread
    assert _server_for(agent, "s1").active.get(thread) == agent._turn_registry.get_active_turn("s1")


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
@pytest.mark.parametrize("holder", ["serving", "stopped-answering"])
async def test_runtime_gen_018_fork_boundary_reads_the_source_sessions_generation(tmp_path, holder):
    """RUNTIME-GEN-018: only the process running the source turn reports it in progress.

    That holds even after one of its requests timed out: the process and its
    turn still run, and any other process would read the turn as interrupted.
    """
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
    source_server.wedged = holder == "stopped-answering"
    target_server.send_request = AsyncMock(
        return_value={"data": [{"id": running, "status": "interrupted"}]}
    )

    # Fork metadata names the durable Session id, never the base session; the
    # running turn is found through the persisted fork boundary.
    agent.controller.session_turns = SimpleNamespace(
        native_turn_id_for_initial_message=lambda session_id, message_id: (
            running if (session_id, message_id) == ("ses-source-durable", "message-1") else None
        )
    )
    still_running, boundary = await agent._fork_source_last_completed_turn_id(
        target_server,
        {
            "source_session_id": "ses-source-durable",
            "source_native_session_id": source_thread,
            "source_message_id": "message-1",
        },
    )

    assert (still_running, boundary) == (True, "turn-done")
    target_server.send_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stuck_on", ["retiring", "current"])
async def test_hfr_144_a_stuck_turn_settles_on_the_generation_that_runs_it(tmp_path, stuck_on):
    """HFR-144: the stuck-turn backstop settles a turn only through the generation holding it.

    That generation is retired, so a native turn that may still run ends with
    its process, while a busy neighbour's turn finishes first.
    """
    agent, cwd = _agent(tmp_path)
    agent.controller.emit_agent_message = AsyncMock()
    await agent.handle_message(_request(cwd, "s1"))
    old = _server_for(agent, "s1")
    if stuck_on == "retiring":
        await agent.renew_runtime(agent.codex_config)
    await agent.handle_message(_request(cwd, "s2"))
    neighbour = _server_for(agent, "s2")
    stuck_turn = agent._turn_registry.get_active_turn("s1")
    running_turn = agent._turn_registry.get_active_turn("s2")
    # S1's turn made no progress for longer than the backstop (1800 s for a
    # 600 s idle timeout); S2 is busy.
    agent._session_last_activity["s1"] = codex_agent_module.time.monotonic() - 10_000

    await agent.evict_idle_transports(600)

    agent.controller.emit_agent_message.assert_awaited_once()
    assert agent._turn_registry.get_active_turn("s1") is None
    assert agent._turn_registry.get_active_turn("s2") == running_turn != stuck_turn
    if stuck_on == "retiring":
        assert not old.alive
        assert agent._units[cwd].current.runtime.transport is neighbour and neighbour.alive
    else:
        assert neighbour is old and agent._units[cwd].current is None and old.alive
        await old.complete(agent._session_mgr.get_thread_id("s2"))
        await _until(lambda: not old.alive)


@pytest.mark.asyncio
async def test_hfr_144_the_stuck_turn_backstop_never_forgets_a_turn_admitted_meanwhile(tmp_path):
    """HFR-144: only the exact stuck turn is forgotten, not the one its Session started meanwhile."""
    agent, cwd = _agent(tmp_path)
    await agent.handle_message(_request(cwd, "s1"))
    await agent.renew_runtime(agent.codex_config)
    await agent.handle_message(_request(cwd, "s2"))
    agent._session_last_activity["s1"] = codex_agent_module.time.monotonic() - 10_000

    async def emit(*_args, **_kwargs):
        # The settled request's terminal result lets S1's next message in.
        await agent.handle_message(_request(cwd, "s1"))

    agent.controller.emit_agent_message = AsyncMock(side_effect=emit)
    await agent.evict_idle_transports(600)

    new_turn = agent._turn_registry.get_active_turn("s1")
    assert new_turn and _server_for(agent, "s1").active.get(agent._session_mgr.get_thread_id("s1")) == new_turn

