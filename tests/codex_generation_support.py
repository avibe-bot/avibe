"""Install fake Codex app-server generations on test agents.

Tests that build a ``CodexAgent`` without ``__init__`` use these helpers to
place a fake transport into a working directory's generation set exactly as a
started app-server would be, without spawning a process.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import OrderedDict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping

import modules.agents.codex.agent as codex_agent_module
from modules.agents.codex.agent import CodexAgent, _CodexRuntime


@dataclass(frozen=True)
class FakeLaunchSpec:
    digest: str
    hub: bool = False

    def close(self) -> None:
        return None


def init_generation_state(agent: Any) -> Any:
    """Give an ``__init__``-less agent the generation state ``__init__`` creates."""
    agent._units = {}
    agent._session_generations = {}
    agent._generation_serials = itertools.count(1)
    agent._runtimes = {}
    agent._shutting_down = False
    agent._instance_serial = next(codex_agent_module._AGENT_INSTANCE_SERIALS)
    agent._runtime_epoch = 0
    agent._reap_tasks = set()
    agent._model_hub_catalogs = OrderedDict()
    agent._model_hub_catalog_lock = asyncio.Lock()
    if not hasattr(agent, "_session_last_activity"):
        agent._session_last_activity = {}
    if not hasattr(agent, "_session_locks"):
        agent._session_locks = {}
    return agent


def codex_agent_shell() -> CodexAgent:
    """A ``CodexAgent`` without ``__init__`` that can hold generations."""
    return init_generation_state(object.__new__(CodexAgent))


def run_unlocked(coro: Any) -> Any:
    """Drive a generation-set call whose lock is free, from sync or async tests."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise RuntimeError("the generation set is busy")


def install_codex_transport(
    agent: Any,
    cwd: str,
    transport: Any,
    *,
    sessions: Mapping[str, str] | None = None,
    digest: str = "installed",
    hub: bool = False,
    activation: Any = None,
    current: bool = True,
    last_activity: float | None = None,
) -> Any:
    """Adopt ``transport`` as a running generation of ``cwd``; return its wrapper.

    ``sessions`` maps base session ids to the thread each has loaded there.
    """
    if not hasattr(agent, "_units"):
        init_generation_state(agent)
    runtime = _CodexRuntime(
        cwd=cwd,
        serial=next(agent._generation_serials),
        transport=transport,
        hub=hub,
        activation=activation,
    )
    if last_activity is not None:
        runtime.last_activity = last_activity
    agent._runtimes.setdefault(cwd, set()).add(runtime)
    unit = agent._unit(cwd)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # A sync test has no loop to run the stop reconciler on.
        unit._kick = lambda: None
    try:
        generation = run_unlocked(unit.adopt(FakeLaunchSpec(digest, hub), runtime, current=current))
    finally:
        unit.__dict__.pop("_kick", None)
    for base_session_id, thread_id in (sessions or {}).items():
        agent._bind_session_thread(generation, base_session_id, thread_id)
    return generation


def bind_installed(agent: Any, generation: Any) -> Any:
    """A binding on an installed generation, as ``_acquire_generation`` returns."""
    return run_unlocked(agent._unit(generation.runtime.cwd).bind(generation))


def codex_transports(agent: Any) -> dict[str, Any]:
    """Each working directory's current transport."""
    return {
        cwd: unit.current.runtime.transport
        for cwd, unit in getattr(agent, "_units", {}).items()
        if unit.current is not None
    }


def acquire_returning(agent: Any, *transports: Any, session: str | None = "session-1") -> Any:
    """A mock ``_acquire_generation`` that serves each transport in turn.

    Each call adopts the next transport as the directory's current generation
    with ``session`` already loaded there, so the turn needs no move. The
    launch load a turn takes at admission only feeds acquisition, so it is
    stubbed too, keeping its Model Hub snapshot.
    """
    from unittest.mock import AsyncMock

    agent._launch_inputs = lambda cwd, *, hub_config=None: SimpleNamespace(hub_config=hub_config)
    queue = list(transports)
    served = itertools.count(1)

    async def acquire(cwd: str, launch: Any = None, *, inputs: Any = None) -> Any:
        generation = install_codex_transport(
            agent,
            cwd,
            queue.pop(0),
            sessions={session: "loaded-thread"} if session else None,
            digest=f"acquired-{next(served)}",
        )
        return bind_installed(agent, generation)

    return AsyncMock(side_effect=acquire)
