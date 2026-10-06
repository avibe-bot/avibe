"""OpenCode runtime generations: launch specs, processes, adoption, and routing.

The OpenCode instance is one runtime unit. Each ``opencode serve`` process is a
generation started from one launch spec; a turn stays on the generation it
started on while new turns move to the generation that serves the current
spec. These tests pin the adapter half of that contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.agents.opencode import client_manager
from modules.agents.opencode import server as opencode_server
from modules.agents.opencode.agent import OpenCodeAgent, _OpenCodeSteerState
from modules.agents.opencode.client_manager import OpenCodeRuntime, compute_launch_spec, read_launch_inputs
from modules.agents.opencode.server import OpenCodeGeneration, OpenCodeLaunchSpec
from tests.fake_pid_helpers import fake_pid

_WRITE_RECORD = OpenCodeGeneration.write_record


@pytest.fixture
def opencode_home(tmp_path, monkeypatch):
    """A home whose OpenCode config, credentials, and binary are the test's own."""

    home = Path.home()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    binary = tmp_path / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\necho 1.18.33\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    probes: list[str] = []

    def probe(path: str) -> str:
        probes.append(path)
        return Path(path).read_text(encoding="utf-8").split()[-1]

    monkeypatch.setattr(client_manager, "_probe_binary_version", probe)
    monkeypatch.setattr(client_manager, "_binary_versions", {})
    config = home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    auth = home / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(binary=binary, config=config, auth=auth, probes=probes)


def _spec(home, *, overlay=None, renew_epoch: int = 0) -> OpenCodeLaunchSpec:
    return compute_launch_spec(read_launch_inputs(str(home.binary), renew_epoch), overlay)


def _current_spec(runtime: OpenCodeRuntime, overlay=None) -> OpenCodeLaunchSpec:
    return asyncio.run(runtime.launch_spec(overlay, runtime.launch_inputs()))


def _overlay(content: str = '{"provider":{"avibe-openai":{}}}'):
    inline = opencode_server._managed_runtime_config_content(content)
    return SimpleNamespace(
        content=content.encode(),
        content_hash=hashlib.sha256(inline.encode()).hexdigest(),
        provider_ids=("avibe-openai",),
    )


# ---------------------------------------------------------------- launch spec


def test_an_in_place_binary_upgrade_changes_the_launch_spec(opencode_home):
    before = _spec(opencode_home)
    again = _spec(opencode_home)
    # The same path now holds a newer build, as an in-place upgrade leaves it.
    opencode_home.binary.write_text("#!/bin/sh\n# upgraded build\necho 1.18.34\n", encoding="utf-8")
    after = _spec(opencode_home)

    assert again.digest == before.digest
    assert after.digest != before.digest
    assert (before.binary_version, after.binary_version) == ("1.18.33", "1.18.34")
    # The version is probed once per file identity, never on every turn.
    assert len(opencode_home.probes) == 2


def test_credential_digest_ignores_token_refresh_but_sees_new_credentials(opencode_home):
    def write_auth(entries):
        opencode_home.auth.write_text(json.dumps(entries), encoding="utf-8")

    write_auth({"anthropic": {"type": "oauth", "access": "a1", "refresh": "r1", "expires": 1}})
    signed_in = _spec(opencode_home)
    # OpenCode refreshes the token in place; each process reads it per request.
    write_auth({"anthropic": {"type": "oauth", "access": "a2", "refresh": "r2", "expires": 2}})
    refreshed = _spec(opencode_home)
    write_auth({
        "anthropic": {"type": "oauth", "access": "a2", "refresh": "r2", "expires": 2},
        "openai": {"type": "api", "key": "sk-one"},
    })
    keyed = _spec(opencode_home)
    write_auth({
        "anthropic": {"type": "oauth", "access": "a2", "refresh": "r2", "expires": 2},
        "openai": {"type": "api", "key": "sk-two"},
    })
    rekeyed = _spec(opencode_home)

    assert refreshed.digest == signed_in.digest
    assert len({signed_in.digest, keyed.digest, rekeyed.digest}) == 3


def test_config_digest_ignores_schema_insertion_but_sees_edits(opencode_home):
    opencode_home.config.write_text('{\n  // comment\n  "provider": {"x": {}}\n}', encoding="utf-8")
    original = _spec(opencode_home)
    opencode_home.config.write_text(
        json.dumps({"$schema": "https://opencode.ai/config.json", "provider": {"x": {}}}),
        encoding="utf-8",
    )
    with_schema = _spec(opencode_home)
    opencode_home.config.write_text(json.dumps({"provider": {"x": {"options": {"baseURL": "https://b"}}}}))
    edited = _spec(opencode_home)

    assert with_schema.digest == original.digest
    assert edited.digest != original.digest


def test_overlay_and_renewal_are_launch_spec_inputs(opencode_home):
    direct = _spec(opencode_home)
    hub = _spec(opencode_home, overlay=_overlay())
    renewed = _spec(opencode_home, renew_epoch=1)

    assert len({direct.digest, hub.digest, renewed.digest}) == 3
    assert hub.overlay_provider_ids == ("avibe-openai",)
    assert json.loads(hub.overlay_inline_content)["provider"] == {"avibe-openai": {}}
    tampered = _overlay()
    tampered.content_hash = "0" * 64
    with pytest.raises(RuntimeError, match="hash changed"):
        _spec(opencode_home, overlay=tampered)


# -------------------------------------------------------------- start & stop


class _Process:
    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode or 0


@pytest.fixture
def isolated_launch(tmp_path, monkeypatch):
    """Spawn fake ``opencode serve`` processes instead of real ones."""

    monkeypatch.setattr(opencode_server, "generation_records_dir", lambda: tmp_path / "generations")
    monkeypatch.setattr(opencode_server, "_OWNED_HERE", {})
    monkeypatch.setattr(opencode_server, "_CHILDREN_HERE", {})
    monkeypatch.setattr(opencode_server, "_ADOPTION_PROBE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(opencode_server, "ensure_plugin_installed", lambda: None)
    monkeypatch.setattr(opencode_server.runtime, "process_create_time", lambda pid: 100.0 + pid)
    monkeypatch.setattr(opencode_server.OpenCodeGeneration, "is_healthy", AsyncMock(return_value=True))
    monkeypatch.setattr(opencode_server, "_pid_listens_on", lambda pid, port: True)
    launches: list[dict] = []
    processes: list[_Process] = []

    async def spawn(*cmd, env, **_kwargs):
        process = processes.pop(0)
        launches.append({"cmd": list(cmd), "env": env, "pid": process.pid})
        return process

    monkeypatch.setattr(opencode_server.asyncio, "create_subprocess_exec", spawn)
    return SimpleNamespace(launches=launches, processes=processes, records=tmp_path / "generations")


def test_a_start_chooses_its_port_and_retries_one_another_process_took(isolated_launch):
    # The first server finds its port taken and exits; the second one serves.
    isolated_launch.processes.extend([_Process(fake_pid(1), returncode=1), _Process(fake_pid(2))])
    spec = OpenCodeLaunchSpec(digest="spec", binary="/bin/opencode")

    generation = asyncio.run(opencode_server.start_generation(spec))

    ports = [
        int(next(arg for arg in launch["cmd"] if arg.startswith("--port=")).split("=", 1)[1])
        for launch in isolated_launch.launches
    ]
    # ``--port=0`` would bind OpenCode's default 4096.
    assert all(port not in (0, 4096) for port in ports)
    assert len(set(ports)) == 2
    assert generation.port == ports[1] and generation.pid == fake_pid(2)
    assert [path.name for path in isolated_launch.records.glob("*.json")] == [f"{generation.generation_id}.json"]
    record = json.loads(generation.record_path.read_text())
    assert record["spec_digest"] == "spec" and record["process_created_at"] == 100.0 + fake_pid(2)
    assert isolated_launch.launches[1]["env"]["AVIBE_OPENCODE_MODEL_HUB"] == "0"


def test_a_hub_start_launches_on_its_own_overlay_copy(isolated_launch):
    isolated_launch.processes.append(_Process(fake_pid(3)))
    overlay = _overlay()
    inline = opencode_server._managed_runtime_config_content(overlay.content)
    spec = OpenCodeLaunchSpec(
        digest="hub",
        binary="/bin/opencode",
        overlay_hash=overlay.content_hash,
        overlay_provider_ids=overlay.provider_ids,
        overlay_file_content=overlay.content,
        overlay_inline_content=inline,
    )

    generation = asyncio.run(opencode_server.start_generation(spec))

    env = isolated_launch.launches[0]["env"]
    assert env["AVIBE_OPENCODE_MODEL_HUB"] == "1"
    assert env["OPENCODE_CONFIG"] == str(generation.overlay_path)
    assert generation.overlay_path.read_bytes() == overlay.content
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == json.loads(inline)
    assert generation.model_hub_provider_ids == ("avibe-openai",)


def test_a_start_that_never_serves_is_reaped_and_leaves_no_record(isolated_launch, monkeypatch):
    isolated_launch.processes.append(_Process(fake_pid(4)))
    monkeypatch.setattr(opencode_server.OpenCodeGeneration, "is_healthy", AsyncMock(return_value=False))
    monkeypatch.setattr(opencode_server, "SERVER_START_TIMEOUT", 0.3)
    stopped: list[int] = []
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: stopped.append(pid) or True)
    overlay = _overlay()
    spec = OpenCodeLaunchSpec(
        digest="hub",
        binary="/bin/opencode",
        overlay_hash=overlay.content_hash,
        overlay_file_content=overlay.content,
        overlay_inline_content=opencode_server._managed_runtime_config_content(overlay.content),
    )

    with pytest.raises(opencode_server.OpenCodeGenerationStartError, match="failed to start"):
        asyncio.run(opencode_server.start_generation(spec))

    assert stopped == [fake_pid(4)]
    assert list(isolated_launch.records.iterdir()) == []


# ------------------------------------------------------------------ adoption


def _record(records: Path, generation_id: str, pid: int, port: int, **fields) -> Path:
    records.mkdir(parents=True, exist_ok=True)
    path = records / f"{generation_id}.json"
    path.write_text(
        json.dumps({
            "generation_id": generation_id,
            "pid": pid,
            "port": port,
            "process_created_at": 100.0 + pid,
            "spec_digest": f"spec-{generation_id}",
            **fields,
        }),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "legacy_shape",
    [
        # Every field a released single-server record could carry.
        {
            "process_created_at": 100.0 + fake_pid(14),
            "caller_context_path": "/old-home/runtime/opencode_caller_context.json",
            "model_hub_overlay_hash": "abc",
            "model_hub_overlay_provider_ids": ["avibe-openai"],
            "owner_pid": 1,
            "runtime_policy_revision": "disable-native-skill-v2",
        },
        # The oldest shape: no birth identity and no caller context.
        {},
    ],
)
def test_adoption_keeps_every_proven_serving_process_and_drops_the_rest(isolated_launch, tmp_path, monkeypatch, legacy_shape):
    records = isolated_launch.records
    serving = _record(records, "ocg_serving", fake_pid(10), 50010, active_run_sessions=["ses_a"])
    dead = _record(records, "ocg_dead", fake_pid(11), 50011)
    reused = _record(records, "ocg_reused", fake_pid(12), 50012)
    unhealthy = _record(records, "ocg_unhealthy", fake_pid(13), 50013)
    legacy = tmp_path / "logs" / "opencode_server.json"
    legacy.parent.mkdir()
    legacy.write_text(
        json.dumps({"pid": fake_pid(14), "port": 4096, "active_run_sessions": ["ses_legacy"], **legacy_shape}),
        encoding="utf-8",
    )
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: legacy)
    alive = {fake_pid(10), fake_pid(12), fake_pid(13), fake_pid(14)}
    ports = {fake_pid(10): 50010, fake_pid(12): 50012, fake_pid(13): 50013, fake_pid(14): 4096}
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(
        opencode_server.runtime,
        "process_create_time",
        # The reused pid now belongs to a process born later.
        lambda pid: 999.0 if pid == fake_pid(12) else 100.0 + pid,
    )
    monkeypatch.setattr(
        opencode_server.runtime,
        "get_process_command",
        lambda pid: f"/bin/opencode serve --hostname=127.0.0.1 --port={ports[pid]}",
    )

    async def healthy(generation):
        return generation.port != 50013

    monkeypatch.setattr(opencode_server.OpenCodeGeneration, "is_healthy", healthy)
    stopped: list[int] = []
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: stopped.append(pid) or True)

    adopted = asyncio.run(opencode_server.adopt_recorded_generations())

    by_pid = {generation.pid: generation for generation in adopted}
    assert set(by_pid) == {fake_pid(10), fake_pid(14)}
    assert by_pid[fake_pid(10)].spec_digest == "spec-ocg_serving"
    assert by_pid[fake_pid(10)].active_run_sessions == {"ses_a"}
    converted = by_pid[fake_pid(14)]
    # A pre-generations server never serves a new turn; it finishes its run.
    assert converted.spec_digest == opencode_server.LEGACY_SPEC_DIGEST
    assert converted.active_run_sessions == {"ses_legacy"}
    assert converted.record_path.exists() and not legacy.exists()
    # Only the proven process that stopped serving is signalled.
    assert stopped == [fake_pid(13)]
    assert serving.exists()
    assert not dead.exists() and not reused.exists() and not unhealthy.exists()


# ------------------------------------------------------- generation lifecycle


def _generation(generation_id: str, index: int, digest: str) -> OpenCodeGeneration:
    return OpenCodeGeneration(
        generation_id=generation_id,
        pid=fake_pid(index),
        port=50100 + index,
        spec_digest=digest,
        process_created_at=1.0,
    )


@pytest.fixture
def fake_processes(tmp_path, monkeypatch):
    """Generations that start and stop without any process."""

    monkeypatch.setattr(opencode_server, "generation_records_dir", lambda: tmp_path / "generations")
    monkeypatch.setattr(opencode_server, "_OWNED_HERE", {})
    monkeypatch.setattr(opencode_server, "_CHILDREN_HERE", {})
    started: list[OpenCodeGeneration] = []
    stopped: list[OpenCodeGeneration] = []
    alive: set[str] = set()

    async def start(spec, **_kwargs):
        generation = _generation(f"ocg_{len(started)}", len(started), spec.digest)
        started.append(generation)
        alive.add(generation.generation_id)
        return generation

    async def stop(generation):
        stopped.append(generation)
        alive.discard(generation.generation_id)

    monkeypatch.setattr(client_manager, "start_generation", start)
    monkeypatch.setattr(client_manager, "stop_generation", stop)
    monkeypatch.setattr(client_manager, "adopt_recorded_generations", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        OpenCodeGeneration,
        "process_alive",
        lambda self: self.generation_id in alive,
    )
    monkeypatch.setattr(OpenCodeGeneration, "write_record", lambda self: None)
    return SimpleNamespace(started=started, stopped=stopped, alive=alive)


def _runtime() -> OpenCodeRuntime:
    return OpenCodeRuntime(SimpleNamespace(binary="opencode", request_timeout_seconds=60))


def test_runtime_gen_020_a_busy_generation_finishes_its_run_while_new_turns_move_on(fake_processes):
    """RUNTIME-GEN-020: a launch change starts a new generation without
    interrupting the run on the old one, and the old one stops only after its
    native run is cleared, not merely when its binding is released."""

    async def scenario():
        runtime = _runtime()
        first = await runtime.acquire(OpenCodeLaunchSpec(digest="v1", binary="opencode"))
        old = first.generation.runtime
        await old.mark_run_active("ses_running")
        second = await runtime.acquire(OpenCodeLaunchSpec(digest="v2", binary="opencode"))
        new = second.generation.runtime
        both_serving = (runtime.current() is new, old in runtime.generations())
        # The turn's binding is gone, but its native run is still recorded.
        await first.release()
        await runtime.reap()
        kept_for_its_run = old in runtime.generations() and old not in fake_processes.stopped
        await old.mark_run_inactive("ses_running")
        await runtime.reap()
        return both_serving, kept_for_its_run, new

    both_serving, kept_for_its_run, new = asyncio.run(scenario())

    assert both_serving == (True, True)
    assert kept_for_its_run
    assert [generation.generation_id for generation in fake_processes.stopped] == ["ocg_0"]
    assert new.generation_id in fake_processes.alive


def test_an_old_generation_stays_until_the_activity_it_started_ends(fake_processes):
    """A launch change retires the old generation, and its turn ends, but a
    background Activity it started still runs there. No binding names that
    Activity, so the graceful stop asks for it by activation and waits."""

    from core.runtime_activation import RuntimeActivationRegistry
    from core.session_activities import SessionActivityRegistry
    from modules.agents.service import AgentService

    activation = RuntimeActivationRegistry()
    service = AgentService(
        SimpleNamespace(),
        activities=SessionActivityRegistry(activation_registry=activation),
        activation_registry=activation,
    )
    agent = _stopping_agent(service, activation)

    async def scenario():
        runtime = agent._runtime
        first = await runtime.acquire(OpenCodeLaunchSpec(digest="v1", binary="opencode"))
        old = first.generation.runtime
        service.activities.start(
            backend="opencode",
            runtime_key="base-1:/work",
            session_id=None,
            activity_id="task-1",
            kind="task",
            activation_identity=old.identity,
        )
        await runtime.acquire(OpenCodeLaunchSpec(digest="v2", binary="opencode"))
        await first.release()
        await runtime.reap()
        kept_for_its_activity = old in runtime.generations() and old not in fake_processes.stopped
        service.activities.complete(
            backend="opencode",
            runtime_key="base-1:/work",
            activity_id="task-1",
            status="completed",
            activation_identity=old.identity,
        )
        await runtime.reap()
        return old, kept_for_its_activity

    old, kept_for_its_activity = asyncio.run(scenario())

    assert kept_for_its_activity
    assert fake_processes.stopped == [old]


def test_runtime_gen_026_a_generation_this_controller_started_is_alive_while_its_handle_says_so(monkeypatch):
    """RUNTIME-GEN-026: its own child's handle decides, never a start-time
    comparison. A clock step moves the start time the platform reports, and the
    generation was then taken for exited, replaced on the next turn, and left
    running with no record."""

    process = SimpleNamespace(returncode=None)
    generation = OpenCodeGeneration(
        generation_id="ocg_own",
        pid=fake_pid(7),
        port=50107,
        spec_digest="v1",
        process_created_at=1_000.0,
        process=process,
    )
    # The start time read now differs from the one recorded at spawn.
    monkeypatch.setattr(opencode_server.runtime, "process_create_time", lambda _pid: 998.0)

    assert generation.process_alive()
    process.returncode = 0
    assert not generation.process_alive()


def test_a_current_generation_whose_process_died_is_replaced(fake_processes):
    async def scenario():
        runtime = _runtime()
        first = await runtime.acquire(OpenCodeLaunchSpec(digest="v1", binary="opencode"))
        await first.release()
        fake_processes.alive.discard(first.generation.runtime.generation_id)
        second = await runtime.acquire(OpenCodeLaunchSpec(digest="v1", binary="opencode"))
        return first.generation.runtime, second.generation.runtime, runtime

    dead, replacement, runtime = asyncio.run(scenario())

    assert replacement is not dead
    assert runtime.generations() == (replacement,)


def test_a_lease_pins_its_generation_until_released_or_expired(fake_processes):
    async def scenario():
        runtime = _runtime()
        lease_id, leased = await runtime.lease(OpenCodeLaunchSpec(digest="v1", binary="opencode"), ttl_seconds=60)
        recorded = dict(leased.leases)
        await runtime.acquire(OpenCodeLaunchSpec(digest="v2", binary="opencode"))
        await runtime.reap()
        pinned = leased in runtime.generations()
        released = await runtime.release_lease(lease_id)
        await runtime.reap()
        expiring_id, expiring = await runtime.lease(OpenCodeLaunchSpec(digest="v3", binary="opencode"), ttl_seconds=1)
        await runtime.acquire(OpenCodeLaunchSpec(digest="v4", binary="opencode"))
        await asyncio.sleep(1.2)
        await runtime.reap()
        return lease_id, recorded, pinned, released, leased, expiring, runtime

    lease_id, recorded, pinned, released, leased, expiring, runtime = asyncio.run(scenario())

    assert lease_id in recorded
    assert pinned and released
    assert leased in fake_processes.stopped and not leased.leases
    assert expiring in fake_processes.stopped


# ------------------------------------------------------------------- routing


class _Generation:
    def __init__(self, generation_id: str) -> None:
        self.generation_id = generation_id
        self.identity = None
        self.active_run_sessions: set[str] = set()
        self.aborted: list[tuple[str, str]] = []
        self.prompts: list[dict] = []

    async def abort_session(self, session_id, directory):
        self.aborted.append((session_id, directory))
        return True

    async def list_messages(self, session_id, directory):
        return []

    async def get_session_status(self, session_id, directory):
        return {"type": "busy"}

    async def prompt_async(self, **kwargs):
        self.prompts.append(kwargs)


def _routing_agent(retiring: _Generation, current: _Generation):
    agent = object.__new__(OpenCodeAgent)
    agent._runtime = SimpleNamespace(current=lambda: current, generations=lambda: (retiring, current))
    agent._session_generations = {"base-old": retiring}
    agent._steering_states = {}
    return agent


def test_runtime_gen_021_a_live_turn_is_aborted_on_its_own_generation():
    """RUNTIME-GEN-021: run state lives in each process, so a session's abort
    and steer go to the generation running its turn, not the current one."""

    retiring, current = _Generation("ocg_old"), _Generation("ocg_new")
    agent = _routing_agent(retiring, current)

    async def scenario():
        task = asyncio.get_running_loop().create_task(asyncio.sleep(10))
        try:
            await agent._abort_active_request("base-old", task, ("native-old", "/work", "route"))
            # A session with no generation has no native run to abort.
            await agent._abort_active_request("base-unbound", task, ("native-x", "/work", "route"))
        finally:
            task.cancel()

    asyncio.run(scenario())

    assert retiring.aborted == [("native-old", "/work")]
    assert current.aborted == []


def test_runtime_gen_021_a_steer_reaches_the_generation_running_the_turn():
    from core.services.agent_steering import SteerOutcome, SteerRequest

    retiring, current = _Generation("ocg_old"), _Generation("ocg_new")
    agent = _routing_agent(retiring, current)
    agent.name = "opencode"
    agent.render_input = lambda text, _metadata=None: text

    async def scenario():
        task = asyncio.get_running_loop().create_task(asyncio.sleep(10))
        agent._active_requests = {"base-old": task}
        agent._session_manager = SimpleNamespace(
            get_request_session=lambda base: ("native-old", "/work", "route") if base == "base-old" else None
        )
        state = _OpenCodeSteerState(
            task=task,
            base_session_id="base-old",
            target_session_id="ses",
            logical_turn_id="turn",
            native_session_id="native-old",
            directory="/work",
            agent="build",
            model=None,
            reasoning_effort=None,
            system=None,
            baseline_message_ids=set(),
            generation=retiring,
        )
        agent._steering_states["base-old"] = state
        try:
            return await agent.steer_active_turn(
                SteerRequest(
                    target_session_id="ses",
                    expected_logical_turn_id="turn",
                    expected_native_turn_id=state.native_turn_id,
                    text="also this",
                ),
                SimpleNamespace(agent_request=None, runtime_key="base-old"),
            )
        finally:
            task.cancel()

    result = asyncio.run(scenario())

    assert result.outcome is SteerOutcome.ACCEPTED
    assert [prompt["text"] for prompt in retiring.prompts] == ["also this"]
    assert current.prompts == []


def test_runtime_gen_022_a_restored_poll_resumes_on_the_generation_that_runs_it():
    """RUNTIME-GEN-022: after a controller restart, a durable poll binds to the
    adopted generation it names. A poll from before generations binds to the
    adopted server that recorded its run; one whose process is gone reads its
    result through the current generation. A poll bound elsewhere than it
    names is rewritten to name that generation, so another restart finds it."""

    named, legacy, current = _Generation("ocg_named"), _Generation("ocg_legacy"), _Generation("ocg_current")
    legacy.active_run_sessions = {"native-legacy"}
    bound: list[object] = []

    async def bind(generation):
        bound.append(generation)
        return SimpleNamespace(generation=SimpleNamespace(runtime=generation))

    async def acquire(_spec):
        bound.append("acquired")
        return SimpleNamespace(generation=SimpleNamespace(runtime=current))

    renamed: dict[str, str] = {}
    agent = object.__new__(OpenCodeAgent)
    agent.controller = SimpleNamespace(model_hub_runtime=None)
    agent.sessions = SimpleNamespace(
        update_active_poll_state=lambda session_id, *, processing_indicator: renamed.update(
            {session_id: processing_indicator["opencode_generation_id"]}
        )
    )
    agent._runtime = SimpleNamespace(
        adopted=True,
        generation=lambda generation_id: {"ocg_named": named, "ocg_legacy": legacy}.get(generation_id),
        generations=lambda: (named, legacy, current),
        bind=bind,
        acquire=acquire,
        launch_inputs=lambda: SimpleNamespace(settings=None),
        launch_spec=AsyncMock(return_value=SimpleNamespace(digest="current")),
    )

    def poll(native_session_id, generation_id=None):
        indicator = {"opencode_generation_id": generation_id} if generation_id else {}
        return SimpleNamespace(opencode_session_id=native_session_id, processing_indicator=indicator)

    async def scenario():
        return [
            (await agent._bind_restored_poll(poll("native-named", "ocg_named"))).generation.runtime,
            (await agent._bind_restored_poll(poll("native-legacy"))).generation.runtime,
            (await agent._bind_restored_poll(poll("native-gone", "ocg_stopped"))).generation.runtime,
        ]

    assert asyncio.run(scenario()) == [named, legacy, current]
    assert bound == [named, legacy, "acquired"]
    assert renamed == {"native-legacy": "ocg_legacy", "native-gone": "ocg_current"}


@pytest.mark.parametrize(("config_save", "renews"), [(True, False), (False, True)])
def test_a_plain_config_save_renews_no_generation(config_save, renews):
    agent = object.__new__(OpenCodeAgent)
    agent.controller = SimpleNamespace(config=SimpleNamespace(opencode=None))
    agent._runtime = SimpleNamespace(config=None, renew=lambda: renewals.append(True), reap=AsyncMock())
    agent._lifecycle_tasks = set()
    renewals: list[bool] = []
    config = SimpleNamespace(binary="opencode")

    async def scenario():
        await agent.renew_runtime(config, config_save=config_save)
        await asyncio.gather(*agent._lifecycle_tasks)

    asyncio.run(scenario())

    assert agent.opencode_config is config and agent._runtime.config is config
    assert bool(renewals) is renews


# ---------------------------------------------------------- UI-process leases


def test_runtime_gen_023_the_ui_process_leases_the_controllers_generation_over_control_ipc():
    """RUNTIME-GEN-023: the UI process never launches OpenCode. It leases the
    controller's current generation and talks to it over HTTP."""

    import httpx

    from core.internal_server import create_app

    generation = _generation("ocg_ui", 30, "spec")
    generation.model_hub_provider_ids = ("avibe-openai",)
    released: list[str] = []

    async def lease_generation(purpose, *, ttl_seconds):
        assert (purpose, ttl_seconds) == ("web OAuth", 960.0)
        return {"lease_id": "ocl_1", "server": generation}

    async def release_generation_lease(lease_id):
        released.append(lease_id)
        return True

    agent = SimpleNamespace(lease_generation=lease_generation, release_generation_lease=release_generation_lease)
    app = create_app(
        SimpleNamespace(
            agent_service=SimpleNamespace(agents={"opencode": agent})
        )
    )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            created = await client.post(
                "/internal/opencode/generation-leases",
                json={"purpose": "web OAuth", "ttl_seconds": 960},
            )
            release = await client.post("/internal/opencode/generation-leases/ocl_1/release")
            invalid = await client.post("/internal/opencode/generation-leases", json={"purpose": "x"})
        return created, release, invalid

    created, release, invalid = asyncio.run(scenario())

    assert created.status_code == 200
    assert created.json() == {
        "ok": True,
        "lease_id": "ocl_1",
        "generation_id": "ocg_ui",
        "base_url": generation.base_url,
        "request_timeout_seconds": 60,
        "model_hub_provider_ids": ["avibe-openai"],
    }
    assert release.json() == {"ok": True, "released": True} and released == ["ocl_1"]
    assert invalid.status_code == 400


def test_a_ui_request_running_past_its_lease_window_keeps_its_generation_until_release(fake_processes, monkeypatch):
    """A UI request on a leased generation runs past the window its lease was
    asked for, while a config change retires that generation. The controller
    keeps the process until the UI releases the lease, and stops it then. The
    leased client starts no request that could outlive the lease."""

    import httpx

    from core.internal_server import create_app
    from modules.agents.opencode.client_manager import lease_opencode_server
    from vibe import internal_client

    runtime = _runtime()

    async def lease_generation(_purpose, *, ttl_seconds):
        lease_id, generation = await runtime.lease(OpenCodeLaunchSpec(digest="v1", binary="opencode"), ttl_seconds)
        return {"lease_id": lease_id, "server": generation}

    app = create_app(
        SimpleNamespace(
            agent_service=SimpleNamespace(
                agents={
                    "opencode": SimpleNamespace(
                        lease_generation=lease_generation, release_generation_lease=runtime.release_lease
                    )
                }
            )
        )
    )

    async def post(path, payload=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post(path, json=payload)
        return {"status_code": response.status_code, "body": response.json()}

    async def create_lease(purpose, *, ttl_seconds):
        return await post("/internal/opencode/generation-leases", {"purpose": purpose, "ttl_seconds": ttl_seconds})

    async def release_lease(lease_id):
        return await post(f"/internal/opencode/generation-leases/{lease_id}/release")

    reached: list[str] = []

    async def no_server_in_this_test(self):
        reached.append(self.base_url)
        raise ConnectionError("no OpenCode server in this test")

    monkeypatch.setattr(internal_client, "create_opencode_generation_lease", create_lease)
    monkeypatch.setattr(internal_client, "release_opencode_generation_lease", release_lease)
    monkeypatch.setattr(opencode_server.OpenCodeServerClient, "_get_http_session", no_server_in_this_test)

    async def scenario():
        # The shortest window a lease is granted for.
        lease = await lease_opencode_server("provider catalog", ttl_seconds=1.0)
        leased = runtime.current()
        # A config change: the next turn starts a new generation, and the
        # leased one retires.
        await runtime.acquire(OpenCodeLaunchSpec(digest="v2", binary="opencode"))
        # The UI request runs past the window the lease was asked for.
        await asyncio.sleep(1.2)
        await runtime.reap()
        during_the_request = list(fake_processes.stopped)
        try:
            await lease.server.get_providers()
        except (TimeoutError, ConnectionError) as exc:
            late_request = type(exc).__name__
        await lease.release()
        await runtime.reap()
        return leased, during_the_request, late_request

    leased, during_the_request, late_request = asyncio.run(scenario())

    assert during_the_request == []
    assert fake_processes.stopped == [leased]
    # Past its window, a request whose timeout could outlive the lease never starts.
    assert (late_request, reached) == ("TimeoutError", [])


def test_a_ui_lease_release_reaches_its_runtime_after_the_backend_is_disabled(fake_processes):
    import httpx

    from core.internal_server import create_app

    runtime = _runtime()

    async def lease_generation(purpose, *, ttl_seconds):
        lease_id, generation = await runtime.lease(OpenCodeLaunchSpec(digest="v1", binary="opencode"), ttl_seconds)
        return {"lease_id": lease_id, "server": generation}

    async def release_generation_lease(lease_id):
        return await runtime.release_lease(lease_id)

    agents = {
        "opencode": SimpleNamespace(
            lease_generation=lease_generation,
            release_generation_lease=release_generation_lease,
        )
    }
    app = create_app(SimpleNamespace(agent_service=SimpleNamespace(agents=agents)))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            created = await client.post(
                "/internal/opencode/generation-leases",
                json={"purpose": "web OAuth", "ttl_seconds": 960},
            )
            # Disabling OpenCode unregisters its agent, then stops its
            # runtime at once; the UI's flow ends with the process.
            agents.pop("opencode")
            await runtime.shutdown()
            stopped_at_disable = list(fake_processes.stopped)
            release = await client.post(f"/internal/opencode/generation-leases/{created.json()['lease_id']}/release")
        return stopped_at_disable, release, runtime

    stopped_at_disable, release, runtime = asyncio.run(scenario())

    assert len(stopped_at_disable) == 1
    # The release still reaches the runtime that held the lease, and frees it.
    assert release.json() == {"ok": True, "released": True}
    assert runtime._leases == {}


def test_a_ui_lease_client_carries_the_generations_private_providers(monkeypatch):
    from vibe import internal_client

    released: list[str] = []
    monkeypatch.setattr(
        internal_client,
        "create_opencode_generation_lease",
        AsyncMock(
            return_value={
                "status_code": 200,
                "body": {
                    "ok": True,
                    "lease_id": "ocl_2",
                    "base_url": "http://127.0.0.1:50999",
                    "request_timeout_seconds": 30,
                    "model_hub_provider_ids": ["avibe-openai"],
                },
            }
        ),
    )
    monkeypatch.setattr(
        internal_client,
        "release_opencode_generation_lease",
        AsyncMock(side_effect=lambda lease_id: released.append(lease_id)),
    )

    async def scenario():
        # A controller-shaped object without an Agent service is a UI process.
        async with client_manager.leased_opencode_server("catalog", controller=SimpleNamespace()) as server:
            return server.base_url, server.model_hub_provider_ids, server.request_timeout_seconds

    assert asyncio.run(scenario()) == ("http://127.0.0.1:50999", ("avibe-openai",), 30)
    assert released == ["ocl_2"]


def test_a_disabled_backend_leases_nothing_in_the_controller():
    async def scenario():
        await client_manager.lease_opencode_server(
            "catalog",
            ttl_seconds=60,
            controller=SimpleNamespace(agent_service=SimpleNamespace(agents={})),
        )

    with pytest.raises(client_manager.OpenCodeRuntimeUnavailableError) as raised:
        asyncio.run(scenario())
    assert raised.value.reason == "opencode_disabled"


def test_records_live_under_the_runtime_dir():
    assert str(opencode_server.generation_records_dir()).startswith(str(os.environ["AVIBE_HOME"]))


# --------------------------------------------------------- review regressions

_LAUNCHER = """
import socket, subprocess, sys
child = subprocess.Popen([sys.executable, "-c", (
    "import socket, sys, time\\n"
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\\n"
    "print(s.getsockname()[1], flush=True); time.sleep(30)\\n"
)], stdout=subprocess.PIPE, text=True)
print(child.stdout.readline().strip(), flush=True)
child.wait()
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX process trees")
def test_a_launcher_whose_child_serves_counts_as_listening():
    # npm shims and .cmd wrappers start the real server as their child.
    import subprocess
    import sys

    launcher = subprocess.Popen(
        [sys.executable, "-c", _LAUNCHER],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        port = int(launcher.stdout.readline())
        assert opencode_server._pid_listens_on(launcher.pid, port)
        assert not opencode_server._pid_listens_on(launcher.pid, port + 1)
    finally:
        opencode_server.terminate_pid_tree_sync(launcher.pid, timeout=2.0)
        launcher.wait(timeout=5)
        launcher.stdout.close()


def test_adoption_leaves_another_runtime_of_this_controller_its_generations(isolated_launch, monkeypatch):
    records = isolated_launch.records
    _record(records, "ocg_crashed", fake_pid(21), 50021)
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: records / "absent.json")
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid == fake_pid(21))
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: "/bin/opencode serve --port=50021")
    isolated_launch.processes.append(_Process(fake_pid(20)))

    spec = OpenCodeLaunchSpec(digest="spec-ocg_crashed", binary="/bin/opencode")

    async def scenario():
        started = await opencode_server.start_generation(OpenCodeLaunchSpec(digest="v1", binary="/bin/opencode"))
        old = _runtime()
        await old.ensure_adopted(spec)
        # The OpenCode backend is disabled and enabled again while the old
        # runtime's turn still runs: that runtime keeps and later stops both.
        new = _runtime()
        await new.ensure_adopted(spec)
        return started, old.generations(), new.generations()

    started, old, new = asyncio.run(scenario())

    assert started.record_path.exists()
    assert [generation.generation_id for generation in old] == ["ocg_crashed"]
    assert new == ()


def test_strict_retirement_retries_a_stop_that_declined_while_a_request_ran(fake_processes):
    async def scenario():
        runtime = _runtime()
        first = await runtime.acquire(OpenCodeLaunchSpec(digest="v1", binary="opencode"))
        old = first.generation.runtime
        old._active_requests = 1
        second = await runtime.acquire(OpenCodeLaunchSpec(digest="v2", binary="opencode"))
        await second.release()
        await first.release()
        await runtime._generations.settled()
        declined = old in runtime.generations()
        old._active_requests = 0
        await runtime.retire_all_strict()
        return declined, runtime

    declined, runtime = asyncio.run(scenario())

    assert declined
    assert runtime.generations() == ()


def test_a_renewal_outlives_a_controller_restart(opencode_home):
    before = OpenCodeRuntime(SimpleNamespace(binary=str(opencode_home.binary), request_timeout_seconds=60))
    original = _current_spec(before)
    before.renew()
    # A restarted controller must not promote a generation the renewal retired.
    after = OpenCodeRuntime(SimpleNamespace(binary=str(opencode_home.binary), request_timeout_seconds=60))

    assert _current_spec(after).digest == _current_spec(before).digest
    assert _current_spec(after).digest != original.digest


def test_shutdown_keeps_the_record_of_a_server_that_survived(isolated_launch, monkeypatch):
    record = _record(isolated_launch.records, "ocg_stuck", fake_pid(22), 50022)
    gone = _record(isolated_launch.records, "ocg_gone", fake_pid(23), 50023)
    # Both were this controller's, the one a runtime here started or adopted.
    for generation_id, pid in (("ocg_stuck", fake_pid(22)), ("ocg_gone", fake_pid(23))):
        opencode_server._OWNED_HERE[generation_id] = (pid, 100.0 + pid)
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: isolated_launch.records / "absent.json")
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid == fake_pid(22))
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: "/bin/opencode serve --port=50022")
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: False)

    opencode_server.stop_owned_generations_sync()

    assert record.exists()
    assert not gone.exists()


# ------------------------------------------------- review round 1 regressions


def test_a_renewal_that_cannot_be_persisted_fails_without_taking_effect(opencode_home, monkeypatch):
    runtime = OpenCodeRuntime(SimpleNamespace(binary=str(opencode_home.binary), request_timeout_seconds=60))
    before = _current_spec(runtime).digest

    def unwritable(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(client_manager, "write_atomic", unwritable)

    # A crash after an unpersisted renewal would promote the retired generation.
    with pytest.raises(OSError):
        runtime.renew()
    assert _current_spec(runtime).digest == before


def test_a_lease_released_before_adoption_never_pins_its_generation(fake_processes, monkeypatch):
    generation = _generation("ocg_leased", 40, "spec")
    generation.leases = {"ocl_ui": 4_000_000_000.0}
    monkeypatch.setattr(client_manager, "adopt_recorded_generations", AsyncMock(return_value=[generation]))
    fake_processes.alive.add(generation.generation_id)
    agent = object.__new__(OpenCodeAgent)
    agent.controller = SimpleNamespace(model_hub_runtime=None)
    agent._runtime = _runtime()
    agent._runtime.launch_spec = AsyncMock(return_value=OpenCodeLaunchSpec(digest="spec", binary="opencode"))

    async def scenario():
        # The UI finishes its OAuth flow right after a controller restart.
        released = await agent.release_generation_lease("ocl_ui")
        wrapper = agent._runtime._wrappers[generation.generation_id]
        return released, wrapper.bindings

    released, bindings = asyncio.run(scenario())

    assert released is True
    assert bindings == 0 and generation.leases == {}


def test_runtime_gen_006_disabling_opencode_stops_every_generation_now_and_refuses_a_late_turn(fake_processes, monkeypatch):
    """RUNTIME-GEN-006, at the OpenCode adapter: disabling OpenCode is the
    user's own interruption. Once the core has interrupted the backend's work
    and unregistered the agent, ``shutdown_runtime()`` stops every generation
    at once, a busy one included. A turn that raced the disable fails with
    the localized retired error instead of starting a process."""

    from modules.agents.base import AgentRequest
    from modules.im import MessageContext
    from vibe.i18n import t as i18n_t

    failures: list[str] = []

    async def emit_backend_failure(_controller, _context, _backend, _error, *, display_text, request, **_kwargs):
        failures.append(display_text)

    monkeypatch.setattr("modules.agents.opencode.agent.emit_backend_failure", emit_backend_failure)
    agent = object.__new__(OpenCodeAgent)
    agent.controller = SimpleNamespace(config=SimpleNamespace(language="zh", platform="slack"))
    agent._runtime = _runtime()
    agent._session_generations = {}
    agent._resource_failures = {}
    agent._remove_ack_reaction = AsyncMock()
    late = AgentRequest(
        context=MessageContext(user_id="U1", channel_id="C1", platform="slack", platform_specific={}),
        message="hello",
        user_message="hello",
        working_path="/work",
        base_session_id="base-late",
        composite_session_id="base-late:/work",
        session_key="slack::channel::C1",
    )

    async def scenario():
        retiring = await agent._runtime.acquire(OpenCodeLaunchSpec(digest="v1", binary="opencode"))
        current = await agent._runtime.acquire(OpenCodeLaunchSpec(digest="v2", binary="opencode"))
        await agent.shutdown_runtime()
        stopped_at_once = set(fake_processes.stopped)
        await agent._process_message(late)
        return {retiring.generation.runtime, current.generation.runtime}, stopped_at_once

    running, stopped_at_once = asyncio.run(scenario())

    assert stopped_at_once == running
    assert len(fake_processes.started) == 2
    assert failures == [f"❌ {i18n_t('error.agentRuntimeRetired', 'zh', agent='OpenCode')}"]


def test_a_start_whose_process_survives_its_stop_keeps_record_and_overlay(isolated_launch, monkeypatch):
    isolated_launch.processes.append(_Process(fake_pid(41)))
    monkeypatch.setattr(opencode_server.OpenCodeGeneration, "is_healthy", AsyncMock(return_value=False))
    monkeypatch.setattr(opencode_server, "SERVER_START_TIMEOUT", 0.3)
    monkeypatch.setattr(opencode_server, "_terminate_started_process", AsyncMock(return_value=False))
    overlay = _overlay()
    spec = OpenCodeLaunchSpec(
        digest="hub",
        binary="/bin/opencode",
        overlay_hash=overlay.content_hash,
        overlay_file_content=overlay.content,
        overlay_inline_content=opencode_server._managed_runtime_config_content(overlay.content),
    )

    with pytest.raises(opencode_server.OpenCodeGenerationStartError):
        asyncio.run(opencode_server.start_generation(spec))

    # The late process stays recorded, so adoption or ``vibe stop`` finds it.
    names = sorted(path.name for path in isolated_launch.records.iterdir())
    assert len(names) == 2 and names[0].endswith(".json") and names[1].endswith(".overlay.json")


def test_adoption_keeps_the_record_of_an_unhealthy_server_that_survives_its_stop(isolated_launch, monkeypatch):
    record = _record(isolated_launch.records, "ocg_stubborn", fake_pid(42), 50042)
    overlay = isolated_launch.records / "ocg_stubborn.overlay.json"
    overlay.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: isolated_launch.records / "absent.json")
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid == fake_pid(42))
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: "/bin/opencode serve --port=50042")
    monkeypatch.setattr(opencode_server.OpenCodeGeneration, "is_healthy", AsyncMock(return_value=False))
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: False)

    assert asyncio.run(opencode_server.adopt_recorded_generations()) == []
    assert record.exists() and overlay.exists()


def test_runtime_gen_024_a_mode_switch_during_a_hub_run_moves_new_turns_to_direct(fake_processes, opencode_home):
    """RUNTIME-GEN-024: switching OpenCode from Hub to Direct while a Hub run is
    live starts a Direct generation for the next turn and lets the Hub
    generation finish its run before it stops."""

    async def scenario():
        runtime = OpenCodeRuntime(SimpleNamespace(binary=str(opencode_home.binary), request_timeout_seconds=60))
        hub_turn = await runtime.acquire(await runtime.launch_spec(_overlay(), runtime.launch_inputs()))
        hub = hub_turn.generation.runtime
        await hub.mark_run_active("ses_hub")
        # The mode switch commits Direct; the next turn's snapshot has no overlay.
        direct_turn = await runtime.acquire(await runtime.launch_spec(None, runtime.launch_inputs()))
        direct = direct_turn.generation.runtime
        both = (hub in runtime.generations(), runtime.current() is direct)
        await hub_turn.release()
        await runtime.reap()
        hub_kept_for_its_run = hub in runtime.generations()
        await hub.mark_run_inactive("ses_hub")
        await runtime.reap()
        return both, hub_kept_for_its_run, hub, direct

    both, hub_kept_for_its_run, hub, direct = asyncio.run(scenario())

    assert both == (True, True)
    assert hub_kept_for_its_run
    assert fake_processes.stopped == [hub]
    assert direct.spec_digest != hub.spec_digest


# ------------------------------------------------- review round 2 regressions


def test_a_confirmed_retirement_of_a_leased_generation_reports_it_draining(fake_processes):
    from modules.agents.opencode.server import StopOutcome

    agent = object.__new__(OpenCodeAgent)
    agent._runtime = _runtime()

    async def scenario():
        lease_id, leased = await agent._runtime.lease(OpenCodeLaunchSpec(digest="v1", binary="opencode"), 60)
        outcome = await agent.retire_current_generation()
        still_running = leased not in fake_processes.stopped
        await agent._runtime.release_lease(lease_id)
        await agent._runtime._generations.settled()
        return outcome, still_running, leased

    outcome, still_running, leased = asyncio.run(scenario())

    # Retiring a leased generation is no stop until the lease drains.
    assert outcome is StopOutcome.DRAINING and still_running
    assert fake_processes.stopped == [leased]


def test_a_confirmed_retirement_of_an_idle_generation_reports_it_stopped(fake_processes):
    from modules.agents.opencode.server import StopOutcome

    agent = object.__new__(OpenCodeAgent)
    agent._runtime = _runtime()

    async def scenario():
        turn = await agent._runtime.acquire(OpenCodeLaunchSpec(digest="v1", binary="opencode"))
        running = turn.generation.runtime
        await turn.release()
        return running, await agent.retire_current_generation(), await agent.retire_current_generation()

    running, stopped, nothing = asyncio.run(scenario())

    assert stopped is StopOutcome.STOPPED
    assert fake_processes.stopped == [running]
    assert nothing is None


def test_an_orphaned_run_marker_is_reconciled_again_by_a_later_sweep(fake_processes, monkeypatch):
    generation = _generation("ocg_orphan", 50, "spec-old")
    generation.active_run_sessions = {"ses_without_poll"}
    fake_processes.alive.add(generation.generation_id)
    monkeypatch.setattr(client_manager, "adopt_recorded_generations", AsyncMock(return_value=[generation]))
    reads = {"count": 0}

    def durable_polls():
        reads["count"] += 1
        if reads["count"] == 1:
            raise OSError("session store briefly unreadable")
        return {}

    runtime = _runtime()
    runtime.durable_poll_generations = durable_polls

    async def scenario():
        # Adoption cannot tell the marker is orphaned, so it keeps it for now.
        await runtime.ensure_adopted(OpenCodeLaunchSpec(digest="spec-new", binary="opencode"))
        pinned = generation in runtime.generations()
        await runtime.reap()
        return pinned

    assert asyncio.run(scenario())
    assert fake_processes.stopped == [generation]
    assert generation.active_run_sessions == set()


# ------------------------------------------------- review round 3 regressions


def test_a_lease_release_whose_write_fails_reaches_the_record_at_the_next_sweep(fake_processes, monkeypatch):
    monkeypatch.setattr(OpenCodeGeneration, "write_record", _WRITE_RECORD)
    real_write = opencode_server.write_atomic
    unwritable = {"now": False}

    def write_atomic(path, content):
        if unwritable["now"]:
            raise OSError("state dir briefly unwritable")
        real_write(path, content)

    monkeypatch.setattr(opencode_server, "write_atomic", write_atomic)

    async def scenario():
        runtime = _runtime()
        lease_id, leased = await runtime.lease(OpenCodeLaunchSpec(digest="v1", binary="opencode"), ttl_seconds=600)
        unwritable["now"] = True
        released = await runtime.release_lease(lease_id)
        bindings = runtime._wrappers[leased.generation_id].bindings
        stale = json.loads(leased.record_path.read_text())["leases"]
        unwritable["now"] = False
        await runtime.reap()
        return lease_id, released, bindings, stale, json.loads(leased.record_path.read_text())["leases"]

    lease_id, released, bindings, stale, swept = asyncio.run(scenario())

    # The release holds the process no longer, though its record still shows it.
    assert released is True and bindings == 0
    assert lease_id in stale
    # A controller adopting the record after the sweep honors no stale lease.
    assert swept == {}


@pytest.mark.parametrize("legacy", [False, True], ids=["generation-record", "legacy-record"])
def test_adoption_tracks_a_serving_process_whose_record_cannot_be_rewritten(
    isolated_launch, tmp_path, monkeypatch, legacy
):
    legacy_path = tmp_path / "opencode_server.json"
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: legacy_path)
    if legacy:
        legacy_path.write_text(json.dumps({"pid": fake_pid(43), "port": 50043}), encoding="utf-8")
        recorded = legacy_path
    else:
        recorded = _record(isolated_launch.records, "ocg_crashed", fake_pid(43), 50043)
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid == fake_pid(43))
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: "/bin/opencode serve --port=50043")
    real_write = opencode_server.write_atomic

    def unwritable(*_args, **_kwargs):
        raise OSError("state dir briefly unwritable")

    monkeypatch.setattr(opencode_server, "write_atomic", unwritable)

    adopted = asyncio.run(opencode_server.adopt_recorded_generations())

    # Tracked, so it counts against the cap and stops once it drains.
    assert [generation.pid for generation in adopted] == [fake_pid(43)]
    # The record that finds it after another crash stays until one replaces it.
    assert recorded.exists()
    monkeypatch.setattr(opencode_server, "write_atomic", real_write)
    adopted[0].flush_record()
    assert adopted[0].record_path.exists()
    assert not legacy_path.exists()


# ------------------------------------------------- review round 5 regressions


def test_an_unreadable_record_is_kept_for_its_possibly_running_process(isolated_launch, monkeypatch):
    records = isolated_launch.records
    records.mkdir(parents=True)
    corrupt = records / "ocg_corrupt.json"
    corrupt.write_text('{"generation_id": "ocg_corrupt", "pid": ', encoding="utf-8")
    overlay = records / "ocg_corrupt.overlay.json"
    overlay.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: records / "absent.json")

    assert asyncio.run(opencode_server.adopt_recorded_generations()) == []
    opencode_server.forget_dead_records()

    # Generation ports are dynamic: without its record nothing finds the server.
    assert corrupt.exists() and overlay.exists()


def test_adoption_applies_resource_governance_to_every_adopted_generation(fake_processes, monkeypatch):
    generation = _generation("ocg_adopted", 44, "spec")
    fake_processes.alive.add(generation.generation_id)
    monkeypatch.setattr(client_manager, "adopt_recorded_generations", AsyncMock(return_value=[generation]))
    governed: list[tuple[int, str]] = []
    runtime = OpenCodeRuntime(
        SimpleNamespace(binary="opencode", request_timeout_seconds=60),
        resource_governor=SimpleNamespace(apply_to_pid=lambda pid, label: governed.append((pid, label))),
    )

    asyncio.run(runtime.ensure_adopted(OpenCodeLaunchSpec(digest="spec", binary="opencode")))

    assert governed == [(generation.pid, "opencode serve")]


def test_an_adoption_cut_short_leaves_every_record_to_its_retry(isolated_launch, monkeypatch):
    records = isolated_launch.records
    for generation_id, index in (("ocg_a", 45), ("ocg_b", 46)):
        _record(records, generation_id, fake_pid(index), 50000 + index, active_run_sessions=[f"ses_{generation_id}"])
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: records / "absent.json")
    ports = {fake_pid(45): 50045, fake_pid(46): 50046}
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid in ports)
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: f"/bin/opencode serve --port={ports[pid]}")
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: True)
    probing_b = asyncio.Event()
    probes = {"ocg_b": 0}

    async def is_healthy(generation):
        if generation.generation_id == "ocg_b":
            probes["ocg_b"] += 1
            if probes["ocg_b"] == 1:
                probing_b.set()
                await asyncio.Event().wait()
        return True

    monkeypatch.setattr(opencode_server.OpenCodeGeneration, "is_healthy", is_healthy)
    runtime = _runtime()
    # Each record's run has a durable poll, so neither generation stops here.
    runtime.durable_poll_generations = lambda: {"ses_ocg_a": "ocg_a", "ses_ocg_b": "ocg_b"}
    spec = OpenCodeLaunchSpec(digest="spec-ocg_b", binary="/bin/opencode")

    async def scenario():
        # A turn's /stop cancels it while adoption probes the second record.
        first = asyncio.get_running_loop().create_task(runtime.ensure_adopted(spec))
        await probing_b.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await runtime.ensure_adopted(spec)
        return {generation.generation_id for generation in runtime.generations()}

    assert asyncio.run(scenario()) == {"ocg_a", "ocg_b"}


# ---------------------------------------------- lifecycle audit regressions


def _leftover_record(records: Path, monkeypatch, generation_id: str, index: int) -> tuple[Path, int]:
    """A server a crashed controller recorded, proven live by its record."""

    pid = fake_pid(index)
    record = _record(records, generation_id, pid, 50000 + index)
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: records / "absent.json")
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(opencode_server.runtime, "process_create_time", lambda candidate: 100.0 + candidate)
    monkeypatch.setattr(
        opencode_server.runtime,
        "get_process_command",
        lambda candidate: f"/bin/opencode serve --port={50000 + index}",
    )
    return record, pid


def test_disabling_the_backend_stops_a_previous_controllers_generations(fake_processes, tmp_path, monkeypatch):
    record, pid = _leftover_record(tmp_path / "generations", monkeypatch, "ocg_leftover", 47)
    stopped: list[int] = []
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda candidate, timeout=5.0: stopped.append(candidate) or True)

    # The backend is disabled before anything used OpenCode since a crash.
    asyncio.run(_runtime().shutdown())

    assert stopped == [pid] and not record.exists()


def test_a_disable_shutdown_that_leaves_a_process_raises_and_its_retry_finishes(fake_processes, tmp_path, monkeypatch):
    """A disable's teardown owner retries a shutdown only if it raises: one
    whose forced stop or leftover stop left a process must not report success.
    The retry stops what is left."""

    record, leftover_pid = _leftover_record(tmp_path / "generations", monkeypatch, "ocg_leftover", 48)
    survives = {"attached": True, "leftover": True}
    stop = client_manager.stop_generation

    async def stop_generation(generation):
        if survives["attached"]:
            raise RuntimeError("process did not exit")
        await stop(generation)

    monkeypatch.setattr(client_manager, "stop_generation", stop_generation)
    monkeypatch.setattr(
        opencode_server,
        "terminate_pid_tree_sync",
        lambda candidate, timeout=5.0: not survives["leftover"],
    )
    runtime = _runtime()

    async def scenario():
        turn = await runtime.acquire(OpenCodeLaunchSpec(digest="v1", binary="opencode"))
        attached = turn.generation.runtime
        with pytest.raises(RuntimeError, match="incomplete"):
            await runtime.shutdown()
        incomplete = (attached in runtime.generations(), record.exists())
        survives.update(attached=False, leftover=False)
        await runtime.shutdown()
        return attached, incomplete

    attached, incomplete = asyncio.run(scenario())

    assert incomplete == (True, True)
    assert fake_processes.stopped == [attached]
    assert runtime.generations() == () and not record.exists()


def _stopping_agent(agent_service, runtime_activation=None) -> OpenCodeAgent:
    """An agent whose forced generation stops settle their work through ``agent_service``."""

    from modules.agents.opencode.session import OpenCodeSessionManager

    agent = object.__new__(OpenCodeAgent)
    agent.controller = SimpleNamespace(agent_service=agent_service, runtime_activation=runtime_activation)
    agent._session_manager = OpenCodeSessionManager(SimpleNamespace(sessions=None), "opencode")
    agent._session_generations = {}
    agent._active_requests = {}
    agent._steering_states = {}
    agent._settling_request_tasks = set()
    agent._runtime = _runtime()
    agent._runtime.on_generation_ready = agent._attach_generation_activation
    agent._runtime.on_generation_stopping = agent._on_generation_stopping
    agent._runtime.holds_activities = agent._generation_holds_activities
    return agent


async def _bind_turn(agent: OpenCodeAgent, digest: str, base: str, workdir: str) -> OpenCodeGeneration:
    binding = await agent._runtime.acquire(OpenCodeLaunchSpec(digest=digest, binary="opencode"))
    agent._session_generations[base] = binding.generation.runtime
    agent._session_manager.set_request_session(base, f"ses-{base}", workdir, "slack::channel::C1")
    return binding.generation.runtime


def test_a_cap_forced_stop_settles_the_turns_and_activities_of_its_process(fake_processes):
    """A fourth launch spec force-stops the oldest busy generation as a
    runtime update: the turn of the session bound to it, and every Activity
    its process started, including one whose session it no longer serves."""

    from core.runtime_activation import RuntimeActivationRegistry
    from core.session_activities import SessionActivityRegistry
    from modules.agents.service import AgentService

    releases: list[tuple[set[str], str]] = []

    async def release_for_backend_refresh(*, backend, base_session_ids, settled_by):
        releases.append((set(base_session_ids), settled_by))

    activation = RuntimeActivationRegistry()
    service = AgentService(
        SimpleNamespace(session_turns=SimpleNamespace(release_for_backend_refresh=release_for_backend_refresh)),
        activities=SessionActivityRegistry(activation_registry=activation),
        activation_registry=activation,
    )
    settled = []
    service.on_activity_terminal = settled.append
    agent = _stopping_agent(service, activation)

    async def scenario():
        generations = [
            await _bind_turn(agent, digest, f"base-{index}", f"/work/{index}")
            for index, digest in enumerate(("v1", "v2", "v3"))
        ]
        oldest = generations[0]
        for runtime_key, activity_id in (("base-0:/work/0", "task-0"), ("base-moved:/work/0", "task-moved")):
            service.activities.start(
                backend="opencode",
                runtime_key=runtime_key,
                session_id=None,
                activity_id=activity_id,
                kind="task",
                activation_identity=oldest.identity,
            )
        await agent._runtime.acquire(OpenCodeLaunchSpec(digest="v4", binary="opencode"))
        await agent._runtime._generations.settled()
        return oldest

    oldest = asyncio.run(scenario())

    assert fake_processes.stopped == [oldest]
    assert releases == [({"base-0"}, "backend_refresh")]
    assert sorted((task.id, task.status, task.metadata.get("interrupt_reason")) for task in settled) == [
        ("task-0", "killed", "backend_refresh"),
        ("task-moved", "killed", "backend_refresh"),
    ]


def test_runtime_gen_006_a_disable_retried_after_a_re_enable_settles_only_the_disabled_agents_work(
    fake_processes, monkeypatch
):
    """RUNTIME-GEN-006, at the OpenCode adapter: the core's interrupt of a
    disable failed, and OpenCode was enabled again before the retry, so the
    retry skips that interrupt. The disabled agent's forced stop settles the
    work bound to its process as disabled, once across every retry, and never
    the re-enabled agent's work: not its Activities under the same runtime
    key, nor the next turn of a session the stop already settled."""

    from core.runtime_activation import RuntimeActivationRegistry
    from core.session_activities import SessionActivityRegistry
    from modules.agents.service import AgentService

    releases: list[tuple[set[str], str]] = []
    terminals = []

    async def release_for_backend_refresh(*, backend, base_session_ids, settled_by):
        releases.append((set(base_session_ids), settled_by))
        if len(releases) == 1:
            # The failure that also made the core's interrupt fail.
            raise RuntimeError("database is locked")

    activation = RuntimeActivationRegistry()
    service = AgentService(
        SimpleNamespace(
            session_turns=SimpleNamespace(release_for_backend_refresh=release_for_backend_refresh),
            scheduled_task_service=SimpleNamespace(settle_activity_runs=terminals.append),
        ),
        activities=SessionActivityRegistry(activation_registry=activation),
        activation_registry=activation,
    )
    # The process survives the first stop that reaches it.
    exits = iter([False, True])
    stop = client_manager.stop_generation

    async def stop_generation(generation):
        if not next(exits):
            raise RuntimeError("process did not exit")
        await stop(generation)

    monkeypatch.setattr(client_manager, "stop_generation", stop_generation)

    def start_task(generation: OpenCodeGeneration, activity_id: str) -> None:
        service.activities.start(
            backend="opencode",
            runtime_key="base-old:/work/old",
            session_id=None,
            activity_id=activity_id,
            kind="task",
            activation_identity=generation.identity,
        )

    disabled = _stopping_agent(service, activation)

    async def shutdown() -> None:
        await disabled.shutdown_runtime(settle_reason="backend_disabled")

    async def scenario():
        old = await _bind_turn(disabled, "v1", "base-old", "/work/old")
        start_task(old, "old-task")
        with pytest.raises(RuntimeError, match="incomplete"):
            await shutdown()
        enabled = _stopping_agent(service, activation)
        new = await _bind_turn(enabled, "v1", "base-new", "/work/new")
        start_task(new, "new-task")
        with pytest.raises(RuntimeError, match="incomplete"):
            await shutdown()
        # The settled session's next turn runs on the re-enabled agent.
        await _bind_turn(enabled, "v1", "base-old", "/work/old")
        start_task(new, "next-task")
        await shutdown()
        return old, new, enabled

    old, new, enabled = asyncio.run(scenario())

    assert releases == [({"base-old"}, "backend_disabled")] * 2
    assert [(task.id, task.status, task.metadata.get("interrupt_reason")) for task in terminals] == [
        ("old-task", "killed", "backend_disabled")
    ]
    assert sorted(task.id for task in service.activities.active_for_runtime("opencode", "base-old:/work/old")) == [
        "new-task",
        "next-task",
    ]
    assert fake_processes.stopped == [old]
    assert enabled._runtime.current() is new and new.generation_id in fake_processes.alive

def test_the_cap_stops_the_oldest_adopted_generation_whatever_its_record_order(fake_processes, monkeypatch):
    """After a restart, records come in filename order, which says nothing
    about age. Adoption keeps process age, so when a new turn's generation
    takes the unit past the cap, the oldest adopted process gives way."""

    def recorded(generation_id: str, index: int, started_at: float) -> OpenCodeGeneration:
        generation = OpenCodeGeneration(
            generation_id=generation_id,
            pid=fake_pid(80 + index),
            port=50180 + index,
            spec_digest=f"old-{index}",
            process_created_at=started_at,
            started_at=started_at,
            # Each still runs a restored turn, so none stops on its own.
            active_run_sessions=(f"ses-{index}",),
        )
        fake_processes.alive.add(generation_id)
        return generation

    young, middle, old = recorded("ocg_0a", 0, 300.0), recorded("ocg_1b", 1, 200.0), recorded("ocg_2c", 2, 100.0)
    monkeypatch.setattr(client_manager, "adopt_recorded_generations", AsyncMock(return_value=[young, middle, old]))
    runtime = _runtime()

    async def scenario():
        await runtime.acquire(OpenCodeLaunchSpec(digest="new", binary="opencode"))
        await runtime._generations.settled()

    asyncio.run(scenario())

    assert [generation.generation_id for generation in fake_processes.stopped] == [old.generation_id]
    assert set(runtime.generations()) == {young, middle, *fake_processes.started}


def test_adoption_takes_a_converted_legacy_server_once(isolated_launch, tmp_path, monkeypatch):
    converted = _record(isolated_launch.records, "ocg_converted", fake_pid(48), 4096)
    # The crash came after the converted record was written and before the
    # legacy file it replaces was removed.
    legacy = tmp_path / "opencode_server.json"
    legacy.write_text(json.dumps({"pid": fake_pid(48), "port": 4096}), encoding="utf-8")
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: legacy)
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid == fake_pid(48))
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: "/bin/opencode serve --port=4096")

    adopted = asyncio.run(opencode_server.adopt_recorded_generations())

    assert [generation.generation_id for generation in adopted] == ["ocg_converted"]
    assert converted.exists() and not legacy.exists()


def test_a_native_migration_never_overlaps_an_opencode_start_outside_a_turn(fake_processes, monkeypatch):
    ready = asyncio.Event()
    starting = asyncio.Event()
    finish_start = asyncio.Event()
    start = client_manager.start_generation

    async def slow_start(spec, **kwargs):
        starting.set()
        await finish_start.wait()
        return await start(spec, **kwargs)

    monkeypatch.setattr(client_manager, "start_generation", slow_start)
    agent = object.__new__(OpenCodeAgent)
    agent.controller = SimpleNamespace(
        model_hub_runtime=None,
        agent_service=SimpleNamespace(
            is_backend_ready=lambda _backend: ready.is_set(),
            wait_backend_ready=lambda _backend: ready.wait(),
        ),
    )
    agent._active_requests = {}
    agent._runtime = _runtime()
    agent._runtime.launch_spec = AsyncMock(return_value=OpenCodeLaunchSpec(digest="v1", binary="opencode"))

    async def settings_request():
        async with agent.current_server():
            pass

    async def scenario():
        ready.set()
        # A settings request is starting a process when the migration checks.
        request = asyncio.get_running_loop().create_task(settings_request())
        await starting.wait()
        busy_while_starting = agent.runtime_has_active_turns()
        # The migration drains the backend; a UI lease now waits for it.
        ready.clear()
        lease = asyncio.get_running_loop().create_task(agent.lease_generation("catalog", ttl_seconds=60))
        finish_start.set()
        await request
        await asyncio.sleep(0)
        held = not lease.done() and len(fake_processes.started) == 1
        ready.set()
        await lease
        return busy_while_starting, held

    busy_while_starting, held = asyncio.run(scenario())

    assert busy_while_starting
    assert held


def test_a_reenabled_backend_leaves_a_legacy_server_another_runtime_here_owns(isolated_launch, tmp_path, monkeypatch):
    legacy = tmp_path / "opencode_server.json"
    legacy.write_text(
        json.dumps({"pid": fake_pid(49), "port": 4096, "active_run_sessions": ["ses_restored"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: legacy)
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid == fake_pid(49))
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: "/bin/opencode serve --port=4096")

    def unwritable(*_args, **_kwargs):
        raise OSError("state dir briefly unwritable")

    # The conversion is deferred, so the legacy file is still the only record.
    monkeypatch.setattr(opencode_server, "write_atomic", unwritable)
    spec = OpenCodeLaunchSpec(digest="current", binary="/bin/opencode")

    async def scenario():
        old = _runtime()
        old.durable_poll_generations = lambda: {"ses_restored": None}
        await old.ensure_adopted(spec)
        # The backend is disabled and enabled again while the restored run continues.
        new = _runtime()
        new.durable_poll_generations = lambda: {"ses_restored": None}
        await new.ensure_adopted(spec)
        return old.generations(), new.generations()

    old, new = asyncio.run(scenario())

    assert [generation.pid for generation in old] == [fake_pid(49)]
    assert new == ()
    assert legacy.exists()


def test_adoption_gives_a_busy_server_more_than_one_health_probe(isolated_launch, monkeypatch):
    _record(isolated_launch.records, "ocg_busy", fake_pid(50), 50050, active_run_sessions=["ses_busy"])
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: isolated_launch.records / "absent.json")
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid == fake_pid(50))
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: "/bin/opencode serve --port=50050")
    probes: list[str] = []

    async def is_healthy(generation):
        # A long tool output stalls the first answer after the crash.
        probes.append(generation.generation_id)
        return len(probes) > 1

    monkeypatch.setattr(opencode_server.OpenCodeGeneration, "is_healthy", is_healthy)
    stopped: list[int] = []
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: stopped.append(pid) or True)

    adopted = asyncio.run(opencode_server.adopt_recorded_generations())

    assert [generation.generation_id for generation in adopted] == ["ocg_busy"]
    assert stopped == []


def test_adoption_never_holds_a_recorded_lease_longer_than_any_lease_is_granted(fake_processes, monkeypatch):
    generation = _generation("ocg_leased", 51, "spec")
    # Written before the clock stepped back across a controller crash.
    generation.leases = {"ocl_far": time.time() + 10 * 86400}
    fake_processes.alive.add(generation.generation_id)
    monkeypatch.setattr(client_manager, "adopt_recorded_generations", AsyncMock(return_value=[generation]))
    runtime = _runtime()

    async def scenario():
        await runtime.ensure_adopted(OpenCodeLaunchSpec(digest="spec", binary="opencode"))
        _binding, timer = runtime._leases["ocl_far"]
        return timer.when() - asyncio.get_running_loop().time()

    assert asyncio.run(scenario()) <= client_manager.MAX_LEASE_SECONDS


@pytest.mark.parametrize("opencode_enabled", [False, True], ids=["disabled", "enabled"])
def test_a_controller_starting_with_opencode_disabled_stops_a_crashed_controllers_servers(
    isolated_launch, monkeypatch, opencode_enabled
):
    """A controller that crashed left a server recorded. Started with OpenCode
    disabled, no agent will ever adopt it, so startup stops it. Started with
    OpenCode enabled, the agent adopts it and its restorable runs."""

    from core import controller as controller_module
    from tests.test_service_readiness import _install_runtime_ready_dependencies

    record = _record(isolated_launch.records, "ocg_left", fake_pid(52), 50052)
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: isolated_launch.records / "absent.json")
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid == fake_pid(52))
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: "/bin/opencode serve --port=50052")
    stopped: list[int] = []
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: stopped.append(pid) or True)
    controller = controller_module.Controller.__new__(controller_module.Controller)
    _install_runtime_ready_dependencies(controller, [])
    from modules.agents.service import AgentService

    controller.agent_service = AgentService(controller=SimpleNamespace())
    if opencode_enabled:
        controller.agent_service.agents["opencode"] = object()
    controller._publish_readiness_unless_im_runtime_failed = lambda: None
    controller._start_model_hub_snapshot_reconcile_loop = lambda: None
    controller.periodic_cleanup = AsyncMock()
    controller.trace_retention_task = None
    controller._agent_events_retention_loop = AsyncMock()

    asyncio.run(controller_module.Controller._on_runtime_ready(controller))

    if opencode_enabled:
        assert stopped == [] and record.exists()
    else:
        assert stopped == [fake_pid(52)] and not record.exists()


def test_a_renewal_during_overlay_preparation_leaves_the_launch_one_coherent_spec(fake_processes, opencode_home):
    """A save that moves the CLI path, with its renewal, and a Model Hub
    catalog save both land while a launch awaits its overlay. That launch
    stays on the spec it snapshotted and starts no spare generation; the next
    launch moves to the new one."""

    upgraded = opencode_home.binary.with_name("opencode-next")
    upgraded.write_bytes(opencode_home.binary.read_bytes() + b"# next\n")
    upgraded.chmod(opencode_home.binary.stat().st_mode)
    hub = {"overlay": _overlay('{"provider":{"avibe-openai":{"models":{"a":{}}}}}')}
    preparing, release = asyncio.Event(), asyncio.Event()
    hold = {"next": False}

    async def prepare_opencode_overlay(*, config):
        # The snapshot is the overlay this launch derives.
        if hold["next"]:
            hold["next"] = False
            preparing.set()
            await release.wait()
        return config

    agent = object.__new__(OpenCodeAgent)
    agent.controller = SimpleNamespace(
        model_hub_runtime=SimpleNamespace(
            snapshot=lambda: hub["overlay"],
            prepare_opencode_overlay=prepare_opencode_overlay,
        ),
        config=SimpleNamespace(opencode=None),
    )
    agent._lifecycle_tasks = set()
    agent._runtime = OpenCodeRuntime(SimpleNamespace(binary=str(opencode_home.binary), request_timeout_seconds=60))

    async def launch():
        async with agent.current_server() as server:
            return server

    async def scenario():
        before = await launch()
        hold["next"] = True
        racing = asyncio.get_running_loop().create_task(launch())
        await preparing.wait()
        hub["overlay"] = _overlay('{"provider":{"avibe-openai":{"models":{"b":{}}}}}')
        await agent.renew_runtime(SimpleNamespace(binary=str(upgraded), request_timeout_seconds=60))
        release.set()
        return before, await racing, await launch()

    before, during, after = asyncio.run(scenario())

    assert during is before
    assert after is not before and after.spec_digest == _spec(
        SimpleNamespace(binary=upgraded), overlay=_overlay('{"provider":{"avibe-openai":{"models":{"b":{}}}}}'), renew_epoch=1
    ).digest
    assert fake_processes.started == [before, after]


def test_a_leased_generation_counts_as_active_runtime_work(fake_processes):
    """A UI lease between its HTTP requests keeps its process in use, so a CLI
    replacement waits for it."""

    agent = object.__new__(OpenCodeAgent)
    agent._active_requests = {}
    agent._runtime = _runtime()

    async def scenario():
        lease_id, _generation = await agent._runtime.lease(
            OpenCodeLaunchSpec(digest="v1", binary="opencode"), ttl_seconds=600
        )
        leased = agent.runtime_has_active_turns()
        await agent._runtime.release_lease(lease_id)
        return leased, agent.runtime_has_active_turns()

    assert asyncio.run(scenario()) == (True, False)


def test_shutdown_stops_only_the_generations_this_controller_owns(isolated_launch, monkeypatch):
    """Another desktop Runtime shares this state directory: stopping this
    controller leaves that Runtime's OpenCode generation running."""

    own = _record(isolated_launch.records, "ocg_own", fake_pid(56), 50056)
    foreign = _record(isolated_launch.records, "ocg_foreign", fake_pid(57), 50057)
    opencode_server._OWNED_HERE["ocg_own"] = (fake_pid(56), 100.0 + fake_pid(56))
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: isolated_launch.records / "absent.json")
    ports = {fake_pid(56): 50056, fake_pid(57): 50057}
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid in ports)
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: f"/bin/opencode serve --port={ports[pid]}")
    stopped: list[int] = []
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: stopped.append(pid) or True)

    opencode_server.stop_owned_generations_sync()

    assert stopped == [fake_pid(56)]
    assert not own.exists() and foreign.exists()


def test_adoption_leaves_every_record_of_another_desktop_runtime_alone(isolated_launch, monkeypatch):
    """Two desktop Runtimes share this state directory. After a restart, this
    controller adopts the server it started, known by the Runtime id its record
    carries, and never adopts, stops, forgets, or cleans another Runtime's
    records: a live one recorded with that Runtime's id, a live one recorded
    before ids were recorded, whose process says it is that Runtime's, and a
    dead one."""

    records = isolated_launch.records
    own_pid, foreign_pid, released_pid, dead_pid = fake_pid(62), fake_pid(63), fake_pid(64), fake_pid(65)
    monkeypatch.setattr(opencode_server, "desktop_caller_provenance", lambda: frozenset({"rt-a"}))
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: records / "absent.json")
    isolated_launch.processes.append(_Process(own_pid))
    started = asyncio.run(opencode_server.start_generation(OpenCodeLaunchSpec(digest="v1", binary="/bin/opencode")))
    foreign = _record(records, "ocg_foreign", foreign_pid, 50063, desktop_runtime_id="rt-b")
    released = _record(records, "ocg_released", released_pid, 50064)
    dead = _record(records, "ocg_dead", dead_pid, 50065, desktop_runtime_id="rt-b")
    overlay = records / "ocg_foreign.overlay.json"
    overlay.write_text("{}", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in records.iterdir()}
    ports = {own_pid: started.port, foreign_pid: 50063, released_pid: 50064}
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid in ports)
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: f"/bin/opencode serve --port={ports[pid]}")
    # A process environment carries its Runtime id; the released record's
    # process belongs to the other Runtime, and so would this controller's own
    # process to a check that read only environments.
    monkeypatch.setattr(
        opencode_server.runtime,
        "_desktop_runtime_mismatch",
        lambda pid, runtime_ids: "runtime_id_mismatch" if pid in (released_pid, own_pid) else None,
    )
    stopped: list[int] = []
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: stopped.append(pid) or True)
    # The restarted controller owns nothing yet.
    opencode_server._OWNED_HERE.clear()

    adopted = asyncio.run(opencode_server.adopt_recorded_generations())

    assert [generation.generation_id for generation in adopted] == [started.generation_id]
    assert stopped == []
    for path in (foreign, released, dead, overlay):
        assert path.read_bytes() == before[path.name]


@pytest.mark.parametrize(
    ("malformed", "ours"),
    [
        ({"desktop_runtime_id": ["rt-a"]}, False),
        ({"desktop_runtime_id": {"id": "rt-a"}}, False),
        ({"generation_id": ["ocg_bad"]}, True),
        ({"process_created_at": 10**400}, True),
    ],
)
def test_a_malformed_record_never_disables_opencode(isolated_launch, monkeypatch, malformed, ours):
    """A readable record whose field has the wrong type degrades to a record
    without that field. Adoption, a lease, a restore's ownership check, and
    shutdown still work, and the record's process is still judged and handled:
    a malformed Runtime id is judged by the live process, which belongs here to
    another Runtime, and a record of this Runtime is adopted and stopped."""

    records = isolated_launch.records
    own_pid, bad_pid = fake_pid(90), fake_pid(91)
    monkeypatch.setattr(opencode_server, "desktop_caller_provenance", lambda: frozenset({"rt-a"}))
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: records / "absent.json")
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid in (own_pid, bad_pid))
    monkeypatch.setattr(opencode_server, "_get_pid_command", lambda pid: None)
    monkeypatch.setattr(
        opencode_server.runtime,
        "_desktop_runtime_mismatch",
        lambda pid, runtime_ids: "runtime_id_mismatch" if pid == bad_pid else None,
    )
    stopped: list[int] = []
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: stopped.append(pid) or True)
    record = _record(records, "ocg_bad", bad_pid, 50091, desktop_runtime_id="rt-a")
    record.write_text(json.dumps({**json.loads(record.read_text()), **malformed}), encoding="utf-8")
    isolated_launch.processes.append(_Process(own_pid))
    runtime = OpenCodeRuntime(SimpleNamespace(binary="/bin/opencode", request_timeout_seconds=60))

    async def lease():
        _lease_id, generation = await runtime.lease(OpenCodeLaunchSpec(digest="v1", binary="/bin/opencode"), 60)
        # An adopted generation of this Runtime retires and, idle, stops.
        await runtime._generations.settled()
        return generation

    leased = asyncio.run(lease())
    foreign = [info["generation_id"] for info in opencode_server.other_runtimes_live_records()]
    opencode_server.stop_owned_generations_sync()

    assert leased.pid == own_pid
    assert foreign == ([] if ours else ["ocg_bad"])
    assert sorted(stopped) == sorted([own_pid, bad_pid] if ours else [own_pid])


def test_a_failed_start_whose_process_survives_is_reaped_until_it_is_gone(isolated_launch, monkeypatch):
    """A start times out and its process survives the stop. The runtime that
    spawned it keeps it owned and stops it at a later reap, record and Hub
    overlay included, instead of leaving it running beside later generations."""

    pid = fake_pid(66)
    isolated_launch.processes.append(_Process(pid))
    monkeypatch.setattr(opencode_server.OpenCodeGeneration, "is_healthy", AsyncMock(return_value=False))
    monkeypatch.setattr(opencode_server, "SERVER_START_TIMEOUT", 0.3)
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: isolated_launch.records / "absent.json")
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda candidate: candidate == pid)
    killable = {"now": False}
    stopped: list[int] = []

    def terminate(candidate, timeout=5.0):
        if killable["now"]:
            stopped.append(candidate)
        return killable["now"]

    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", terminate)
    monkeypatch.setattr(opencode_server, "_terminate_started_process", AsyncMock(return_value=False))
    overlay = _overlay()
    spec = OpenCodeLaunchSpec(
        digest="hub",
        binary="/bin/opencode",
        overlay_hash=overlay.content_hash,
        overlay_file_content=overlay.content,
        overlay_inline_content=opencode_server._managed_runtime_config_content(overlay.content),
    )
    runtime = _runtime()

    async def scenario():
        with pytest.raises(opencode_server.OpenCodeGenerationStartError):
            await runtime.acquire(spec)
        await runtime.reap()
        still_running = sorted(path.name for path in isolated_launch.records.iterdir())
        killable["now"] = True
        await runtime.reap()
        return still_running

    still_running = asyncio.run(scenario())

    assert len(still_running) == 2
    assert stopped == [pid]
    assert list(isolated_launch.records.iterdir()) == []


def test_service_shutdown_stops_a_started_process_whose_record_was_never_written(isolated_launch, monkeypatch):
    """A start's record write fails and its process survives the stop. Avibe
    stops before any reap retries it: the controller's shutdown still stops
    that process and drops its Hub overlay. It is this controller's own child,
    so its handle decides, never the start time the platform reports now: a
    clock step does not spare one, and one that exited is not signalled."""

    proven, shifted, exited = fake_pid(67), fake_pid(68), fake_pid(69)
    isolated_launch.processes.extend(_Process(pid) for pid in (proven, shifted, exited))
    create_times = {proven: 1.0, shifted: 2.0, exited: 3.0}
    monkeypatch.setattr(opencode_server.runtime, "process_create_time", create_times.get)
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid in create_times)
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: isolated_launch.records / "absent.json")

    def disk_full(_generation):
        raise OSError("No space left on device")

    monkeypatch.setattr(OpenCodeGeneration, "write_record", disk_full)
    monkeypatch.setattr(opencode_server, "_terminate_started_process", AsyncMock(return_value=False))
    stopped: list[int] = []
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: stopped.append(pid) or True)
    overlay = _overlay()
    spec = OpenCodeLaunchSpec(
        digest="hub",
        binary="/bin/opencode",
        overlay_hash=overlay.content_hash,
        overlay_file_content=overlay.content,
        overlay_inline_content=opencode_server._managed_runtime_config_content(overlay.content),
    )
    survivors: list[OpenCodeGeneration] = []

    async def start_three():
        for _ in range(3):
            with pytest.raises(OSError, match="No space"):
                await opencode_server.start_generation(spec, on_survivor=survivors.append)

    asyncio.run(start_three())
    create_times[shifted] = 9.0  # The clock stepped.
    next(generation for generation in survivors if generation.pid == exited)._process.returncode = 0

    opencode_server.stop_owned_generations_sync()

    assert stopped == [proven, shifted]
    assert opencode_server._OWNED_HERE == {} and opencode_server._CHILDREN_HERE == {}
    assert list(isolated_launch.records.iterdir()) == []


def test_runtime_gen_026_service_shutdown_stops_its_own_recorded_server_after_a_clock_step(isolated_launch, monkeypatch):
    """RUNTIME-GEN-026: the start time the platform reports for a server this
    controller started moved with the clock, so its record no longer proves
    it. Its handle still does: shutdown stops it and forgets its record."""

    pid = fake_pid(70)
    isolated_launch.processes.append(_Process(pid))
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: isolated_launch.records / "absent.json")
    stopped: list[int] = []
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda target, timeout=5.0: stopped.append(target) or True)

    generation = asyncio.run(
        opencode_server.start_generation(OpenCodeLaunchSpec(digest="spec", binary="/bin/opencode"))
    )
    assert generation.record_path.exists()
    monkeypatch.setattr(opencode_server.runtime, "process_create_time", lambda _pid: 7.0)

    opencode_server.stop_owned_generations_sync()

    assert stopped == [pid]
    assert not generation.record_path.exists()
    assert opencode_server._OWNED_HERE == {} and opencode_server._CHILDREN_HERE == {}


def test_a_turn_keeps_the_opencode_settings_it_was_admitted_with(monkeypatch):
    """An ``agents.opencode`` save lands after a turn's admission snapshot and
    before the turn resolves its model. The turn runs on the generation its
    snapshot names, so it also takes its default provider, reasoning effort,
    and poll settings from that snapshot; the next turn moves to the new ones."""

    from modules.agents.base import AgentRequest
    from modules.im import MessageContext
    from tests.opencode_generation_fakes import serve_opencode_agent

    admitted = SimpleNamespace(
        default_provider="openai",
        default_reasoning_effort="high",
        error_retry_limit=0,
        active_turn_timeout_seconds=0,
    )
    saved = SimpleNamespace(
        default_provider="anthropic",
        default_reasoning_effort="low",
        error_retry_limit=5,
        active_turn_timeout_seconds=600,
    )
    prompts: list[dict] = []
    polled: list[object] = []
    bootstrapping, proceed = asyncio.Event(), asyncio.Event()
    agent = object.__new__(OpenCodeAgent)

    class _Server:
        async def ensure_directory_ready(self, directory):
            bootstrapping.set()
            await proceed.wait()

        async def list_messages(self, session_id, directory):
            return []

        async def get_available_models(self, directory):
            return {"providers": []}

        async def prompt_async(self, **kwargs):
            prompts.append(kwargs)

        async def mark_run_active(self, session_id):
            return None

        async def mark_run_inactive(self, session_id):
            return None

        def get_default_agent_from_config(self):
            return None

        def get_agent_model_from_config(self, _agent):
            return None

        def get_agent_reasoning_effort_from_config(self, _agent):
            return None

    class _SessionManager:
        async def ensure_working_dir(self, path):
            return None

        async def get_or_create_session_id(self, request, server):
            return "oc-session"

        def set_request_session(self, *args):
            return None

        def set_agent_session_id(self, *_args):
            return None

        def mark_initialized(self, session_id):
            return False

    class _Sessions:
        def add_active_poll(self, **kwargs):
            return None

        def remove_active_poll(self, session_id):
            return None

        def update_active_poll_state(self, session_id, **kwargs):
            return None

    class _PollLoop:
        async def run_prompt_poll(self, *args, settings=None, **kwargs):
            polled.append(settings)
            return "done", False

    async def _async_noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("modules.agents.opencode.agent.build_system_prompt_injection", lambda **kwargs: "")
    monkeypatch.setattr("modules.agents.opencode.agent.bind_caller_context_session", lambda *args, **kwargs: None)
    agent.controller = SimpleNamespace(
        config=SimpleNamespace(platform="slack", reply_enhancements=False, remote_access=None, language="en", opencode=admitted),
        processing_indicator=SimpleNamespace(snapshot_request=lambda request: {}),
        get_opencode_overrides=lambda context: (None, "gpt-5.4", None),
    )
    agent.config = agent.controller.config
    agent.sessions = _Sessions()
    agent.opencode_config = admitted
    agent._session_manager = _SessionManager()
    agent._poll_loop = _PollLoop()
    agent._steering_states = {}
    agent._active_requests = {}
    serve_opencode_agent(agent, _Server())
    agent._delete_ack = _async_noop
    agent._remove_ack_reaction = _async_noop
    request = AgentRequest(
        context=MessageContext(user_id="u", channel_id="c", platform="slack", platform_specific={}),
        message="hello",
        user_message="hello",
        working_path="/tmp/work",
        base_session_id="base",
        composite_session_id="base:/tmp/work",
        session_key="slack::c",
    )

    async def scenario():
        turn = asyncio.get_running_loop().create_task(agent._process_message(request))
        await bootstrapping.wait()
        # A save of agents.opencode adopts its config the way a renewal does.
        agent._adopt_runtime_config(saved)
        proceed.set()
        await turn

    asyncio.run(scenario())

    assert prompts and prompts[0]["model"] == {"providerID": "openai", "modelID": "gpt-5.4"}
    assert prompts[0]["reasoning_effort"] == "high"
    assert polled == [admitted]
