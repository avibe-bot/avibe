"""A one-generation stand-in for the OpenCode runtime, serving a fake server.

Adapter tests that exercise a turn, a stop, or a restored poll need the agent
to bind work to some server. The stand-in records every acquire, bind, and
release, so a test can assert that work held its generation exactly as long as
it ran.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeBinding:
    def __init__(self, server: Any) -> None:
        self.generation = SimpleNamespace(runtime=server, stopped=False)
        self.released = False

    async def release(self) -> None:
        self.released = True


class FakeOpenCodeRuntime:
    def __init__(self, server: Any, agent: Any = None) -> None:
        self.server = server
        self._agent = agent
        self.bindings: list[FakeBinding] = []
        self.specs: list[Any] = []
        self.last_start_failure_pid = None
        self.start_failures = 0
        self.adopted = True
        self.outside_turn_acquisitions = 0

    def launch_inputs(self) -> Any:
        # The real runtime's config is the agent's ``agents.opencode`` object.
        agent = self._agent
        settings = getattr(agent, "opencode_config", None) or getattr(
            getattr(getattr(agent, "controller", None), "config", None), "opencode", None
        )
        return SimpleNamespace(settings=settings)

    async def launch_spec(self, overlay: Any, inputs: Any) -> Any:
        spec = SimpleNamespace(digest="spec", overlay=overlay)
        self.specs.append(spec)
        return spec

    async def acquire(self, spec: Any) -> FakeBinding:
        binding = FakeBinding(self.server)
        self.bindings.append(binding)
        return binding

    async def bind(self, generation: Any) -> FakeBinding:
        binding = FakeBinding(generation)
        self.bindings.append(binding)
        return binding

    async def ensure_adopted(self) -> None:
        return None

    def current(self) -> Any:
        return self.server

    def has_bound_work(self) -> bool:
        return any(not binding.released for binding in self.bindings)

    def generations(self) -> tuple[Any, ...]:
        return (self.server,) if self.server is not None else ()

    def generation(self, generation_id: str) -> Any:
        if self.server is not None and getattr(self.server, "generation_id", None) == generation_id:
            return self.server
        return None

    async def reap(self) -> None:
        return None


def serve_opencode_agent(agent: Any, server: Any) -> FakeOpenCodeRuntime:
    """Make ``agent`` run every turn on ``server`` as its only generation."""

    if server is not None and not hasattr(server, "generation_id"):
        server.generation_id = "ocg_test"
    if server is not None and not hasattr(server, "active_run_sessions"):
        server.active_run_sessions = set()
    runtime = FakeOpenCodeRuntime(server, agent)
    agent._runtime = runtime
    agent._session_generations = {}
    agent._lifecycle_tasks = set()
    agent._resource_failures = {}
    return runtime


class FakeServerLease:
    """A lease on ``server``, recording whether its holder released it."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.lease_id = "ocl_test"
        self.released = False

    async def release(self) -> None:
        self.released = True


def lease_returning(server: Any):
    """An ``AgentAuthService._lease_opencode_server`` stand-in that leases ``server``.

    Its ``leases`` list holds every lease it handed out, and ``ttls`` the
    lifetime each one asked for.
    """

    leases: list[FakeServerLease] = []
    ttls: list[float] = []

    async def _lease(_purpose: str, *, ttl_seconds: float) -> FakeServerLease | None:
        ttls.append(ttl_seconds)
        if server is None:
            return None
        lease = FakeServerLease(server)
        leases.append(lease)
        return lease

    _lease.leases = leases  # type: ignore[attr-defined]
    _lease.ttls = ttls  # type: ignore[attr-defined]
    return _lease


def ui_lease(get_server):
    """A ``vibe.api._opencode_lease`` stand-in over an async ``get_server`` factory.

    A factory returning ``None`` means OpenCode is disabled; one that raises
    propagates, as an unreachable controller does.
    """

    async def _lease(_purpose: str) -> FakeServerLease | None:
        server = await get_server()
        return None if server is None else FakeServerLease(server)

    return _lease


def lease_via(get_server):
    """A ``lease_opencode_server`` stand-in that leases what ``get_server()`` returns.

    Its ``leases`` list holds every lease it handed out.
    """

    leases: list[FakeServerLease] = []

    async def _lease(_purpose: str, *, ttl_seconds: float, controller: Any = None) -> FakeServerLease:
        del ttl_seconds, controller
        lease = FakeServerLease(await get_server())
        leases.append(lease)
        return lease

    _lease.leases = leases  # type: ignore[attr-defined]
    return _lease
