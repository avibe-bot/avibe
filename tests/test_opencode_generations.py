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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.agents.opencode import client_manager
from modules.agents.opencode import server as opencode_server
from modules.agents.opencode.agent import OpenCodeAgent, _OpenCodeSteerState
from modules.agents.opencode.client_manager import OpenCodeRuntime, compute_launch_spec
from modules.agents.opencode.server import OpenCodeGeneration, OpenCodeLaunchSpec
from tests.fake_pid_helpers import fake_pid


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
    return compute_launch_spec(str(home.binary), overlay, renew_epoch)


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
    result through the current generation."""

    named, legacy, current = _Generation("ocg_named"), _Generation("ocg_legacy"), _Generation("ocg_current")
    legacy.active_run_sessions = {"native-legacy"}
    bound: list[object] = []

    async def bind(generation):
        bound.append(generation)
        return SimpleNamespace(generation=SimpleNamespace(runtime=generation))

    async def acquire(_spec):
        bound.append("acquired")
        return SimpleNamespace(generation=SimpleNamespace(runtime=current))

    agent = object.__new__(OpenCodeAgent)
    agent.controller = SimpleNamespace(model_hub_runtime=None)
    agent._runtime = SimpleNamespace(
        adopted=True,
        generation=lambda generation_id: {"ocg_named": named, "ocg_legacy": legacy}.get(generation_id),
        generations=lambda: (named, legacy, current),
        bind=bind,
        acquire=acquire,
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
    app = create_app(SimpleNamespace(agent_service=SimpleNamespace(agents={"opencode": agent})))

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
    sibling = _record(records, "ocg_sibling", fake_pid(20), 50020, owner_pid=os.getpid())
    crashed = _record(records, "ocg_crashed", fake_pid(21), 50021, owner_pid=fake_pid(99))
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: records / "absent.json")
    ports = {fake_pid(20): 50020, fake_pid(21): 50021}
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid in ports)
    monkeypatch.setattr(
        opencode_server.runtime,
        "get_process_command",
        lambda pid: f"/bin/opencode serve --port={ports[pid]}",
    )

    adopted = asyncio.run(opencode_server.adopt_recorded_generations())

    # The OpenCode backend was disabled and enabled again while its old
    # runtime's turn still runs: that runtime keeps and later stops its process.
    assert [generation.generation_id for generation in adopted] == ["ocg_crashed"]
    assert json.loads(sibling.read_text())["owner_pid"] == os.getpid()
    assert json.loads(crashed.read_text())["owner_pid"] == os.getpid()


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
    original = asyncio.run(before.launch_spec(None))
    before.renew()
    # A restarted controller must not promote a generation the renewal retired.
    after = OpenCodeRuntime(SimpleNamespace(binary=str(opencode_home.binary), request_timeout_seconds=60))

    assert asyncio.run(after.launch_spec(None)).digest == asyncio.run(before.launch_spec(None)).digest
    assert asyncio.run(after.launch_spec(None)).digest != original.digest


def test_shutdown_keeps_the_record_of_a_server_that_survived(isolated_launch, monkeypatch):
    record = _record(isolated_launch.records, "ocg_stuck", fake_pid(22), 50022)
    gone = _record(isolated_launch.records, "ocg_gone", fake_pid(23), 50023)
    monkeypatch.setattr(opencode_server, "legacy_pid_file", lambda: isolated_launch.records / "absent.json")
    monkeypatch.setattr(opencode_server.runtime, "pid_alive", lambda pid: pid == fake_pid(22))
    monkeypatch.setattr(opencode_server.runtime, "get_process_command", lambda pid: "/bin/opencode serve --port=50022")
    monkeypatch.setattr(opencode_server, "terminate_pid_tree_sync", lambda pid, timeout=5.0: False)

    opencode_server.terminate_recorded_generations_sync()

    assert record.exists()
    assert not gone.exists()
