from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from core.runtime_activation import (
    RuntimeActivationCommit,
    RuntimeActivationRegistry,
    RuntimeActivationResolution,
)
from core.runtime_recovery import FallbackRequestRecoveryHandler
from core.scheduled_tasks import ScheduledTaskService, TaskExecutionRequest, TaskExecutionStore
from core.session_activities import SessionActivityRegistry
from modules.agents.base import AGENT_RUNTIME_TURN_KEY, AGENT_RUNTIME_TURN_TOKEN
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.codex.agent import CodexAgent
from modules.agents.opencode.agent import OpenCodeAgent
from modules.agents.opencode.server import OpenCodeGeneration
from modules.agents.service import AgentService
from tests.codex_generation_support import init_generation_state, install_codex_transport
from tests.fake_pid_helpers import fake_pid


def _join(thread: threading.Thread) -> None:
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_admission_first_commit_finishes_before_retirement_predicate() -> None:
    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    commit_entered = threading.Event()
    allow_commit = threading.Event()
    retire_started = threading.Event()
    order: list[str] = []
    admission: list[RuntimeActivationCommit[str]] = []
    retired: list[bool] = []

    def commit() -> str:
        order.append("commit-entered")
        commit_entered.set()
        assert allow_commit.wait(timeout=2)
        order.append("commit-finished")
        return "owner"

    def admit() -> None:
        admission.append(registry.commit_if_current(identity, commit))

    def retire() -> None:
        retire_started.set()

        def final_predicate() -> bool:
            order.append("retire-predicate")
            return True

        retired.append(registry.retire_if_current(identity, final_predicate))

    admission_thread = threading.Thread(target=admit)
    retirement_thread = threading.Thread(target=retire)
    admission_thread.start()
    assert commit_entered.wait(timeout=2)
    retirement_thread.start()
    assert retire_started.wait(timeout=2)
    allow_commit.set()

    _join(admission_thread)
    _join(retirement_thread)

    assert admission == [RuntimeActivationCommit(admitted=True, value="owner")]
    assert retired == [True]
    assert order == ["commit-entered", "commit-finished", "retire-predicate"]
    assert registry.current("claude", "runtime-1") is None
    assert registry.current("claude", "runtime-1", include_retired=True) == identity


def test_task_request_serialization_excludes_the_live_activation_boundary() -> None:
    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    request = TaskExecutionRequest(
        id="run-1",
        request_type="agent_run",
        observed_activation_identity=identity,
    )

    payload = request.to_dict()

    assert "observed_activation_identity" not in payload
    assert payload["id"] == "run-1"


def test_cleanup_first_retirement_rejects_delayed_owner_commit() -> None:
    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    predicate_entered = threading.Event()
    allow_retirement = threading.Event()
    admission_started = threading.Event()
    commit_called = threading.Event()
    admission: list[RuntimeActivationCommit[str]] = []
    retired: list[bool] = []

    def final_predicate() -> bool:
        predicate_entered.set()
        assert allow_retirement.wait(timeout=2)
        return True

    def retire() -> None:
        retired.append(registry.retire_if_current(identity, final_predicate))

    def admit() -> None:
        admission_started.set()
        admission.append(
            registry.commit_if_current(
                identity,
                lambda: commit_called.set() or "owner",
            )
        )

    retirement_thread = threading.Thread(target=retire)
    admission_thread = threading.Thread(target=admit)
    retirement_thread.start()
    assert predicate_entered.wait(timeout=2)
    admission_thread.start()
    assert admission_started.wait(timeout=2)
    allow_retirement.set()

    _join(retirement_thread)
    _join(admission_thread)

    assert retired == [True]
    assert admission == [RuntimeActivationCommit(admitted=False)]
    assert not commit_called.is_set()


def test_replacement_generation_rejects_old_identity() -> None:
    registry = RuntimeActivationRegistry()
    old_identity = registry.attach("codex", "/workspace")
    current_identity = registry.attach("codex", "/workspace")
    old_commit_called = False

    def old_commit() -> None:
        nonlocal old_commit_called
        old_commit_called = True

    old_result = registry.commit_if_current(old_identity, old_commit)
    current_result = registry.commit_if_current(current_identity, lambda: "started")

    assert old_result == RuntimeActivationCommit(admitted=False)
    assert old_commit_called is False
    assert current_result == RuntimeActivationCommit(admitted=True, value="started")
    assert registry.current("codex", "/workspace") == current_identity


def test_retired_activation_targets_are_bounded_without_admitting_old_identity() -> None:
    registry = RuntimeActivationRegistry()
    oldest = registry.attach("codex", "/workspace/0")
    assert registry.retire_if_current(oldest, lambda: True)

    for index in range(1, 200):
        identity = registry.attach("codex", f"/workspace/{index}")
        assert registry.retire_if_current(identity, lambda: True)

    commit = Mock(return_value="started")
    assert not registry.commit_if_current(oldest, commit).admitted
    commit.assert_not_called()
    assert registry.tracked_target_count() == 0
    assert registry.retired_diagnostic_count() <= 64


def test_retirement_reservation_rejects_admission_until_abort() -> None:
    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    reservation = registry.reserve_retirement(identity)

    assert reservation is not None
    rejected = Mock(return_value="owner")
    assert not registry.commit_if_current(identity, rejected).admitted
    rejected.assert_not_called()

    assert registry.finish_retirement(reservation, retire=False)
    admitted = registry.commit_if_current(identity, lambda: "owner")
    assert admitted == RuntimeActivationCommit(admitted=True, value="owner")


def test_retirement_reservation_can_retire_and_release_registry_target() -> None:
    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    reservation = registry.reserve_retirement(identity)

    assert reservation is not None
    assert registry.finish_retirement(reservation, retire=True)
    assert registry.current("claude", "runtime-1") is None
    assert registry.current("claude", "runtime-1", include_retired=True) == identity
    assert registry.tracked_target_count() == 0


def test_backend_binding_resolvers_use_exact_anchor_and_workdir() -> None:
    registry = RuntimeActivationRegistry()

    claude_identity = registry.attach("claude", "anchor:/work")
    claude_client = SimpleNamespace(
        _vibe_runtime_activation_identity=claude_identity,
        _vibe_runtime_base_session_id="anchor",
        _vibe_runtime_workdir="/work",
    )
    claude = object.__new__(ClaudeAgent)
    claude.claude_sessions = {"opaque-runtime-key": claude_client}
    assert claude.runtime_activation_identity_for_session_binding(
        session_anchor="anchor",
        workdir="/work",
    ) == claude_identity
    assert claude.runtime_activation_identity_for_session_binding(
        session_anchor="missing",
        workdir=None,
    ) is None
    with pytest.raises(ValueError, match="changed workdir"):
        claude.runtime_activation_identity_for_session_binding(
            session_anchor="anchor",
            workdir="/other",
        )

    codex_identity = registry.attach("codex", "/work#1")
    codex = init_generation_state(object.__new__(CodexAgent))
    install_codex_transport(
        codex, "/work", SimpleNamespace(), activation=codex_identity, sessions={"anchor": "thread"}
    )
    codex._session_mgr = SimpleNamespace(
        get_cwd=lambda anchor: "/work" if anchor == "anchor" else None,
    )
    assert codex.runtime_activation_identity_for_session_binding(
        session_anchor="anchor",
        workdir="/work",
    ) == codex_identity
    with pytest.raises(ValueError, match="changed workdir"):
        codex.runtime_activation_identity_for_session_binding(
            session_anchor="anchor",
            workdir="/other",
        )


def test_opencode_binding_resolver_follows_each_sessions_generation() -> None:
    registry = RuntimeActivationRegistry()

    def generation(generation_id: str, port: int) -> OpenCodeGeneration:
        return OpenCodeGeneration(
            generation_id=generation_id,
            pid=fake_pid(port),
            port=port,
            spec_digest=generation_id,
            process_created_at=1.0,
        )

    retiring = generation("ocg_old", 1)
    current = generation("ocg_new", 2)
    opencode = object.__new__(OpenCodeAgent)
    opencode.controller = SimpleNamespace(runtime_activation=registry)
    opencode._runtime = SimpleNamespace(current=lambda: current)
    opencode._session_generations = {"anchor": retiring}
    opencode._session_manager = SimpleNamespace(
        get_request_session=lambda anchor: (
            ("", "/work", "route") if anchor == "anchor" else None
        )
    )
    opencode._attach_generation_activation(retiring)
    opencode._attach_generation_activation(current)

    # A session's live turn commits against the process that runs it; any
    # other session would start its next turn on the current generation.
    assert retiring.identity != current.identity
    assert opencode.runtime_activation_identity_for_session_binding(
        session_anchor="anchor",
        workdir="/work",
    ) == retiring.identity
    assert opencode.runtime_activation_identity_for_request(
        SimpleNamespace(base_session_id="anchor")
    ) == retiring.identity
    assert opencode.runtime_activation_identity_for_session_binding(
        session_anchor="other",
        workdir="/work",
    ) == current.identity
    with pytest.raises(ValueError, match="changed workdir"):
        opencode.runtime_activation_identity_for_session_binding(
            session_anchor="anchor",
            workdir="/other",
        )

    asyncio.run(opencode._on_generation_stopping(retiring, False))

    assert registry.commit_if_current(retiring.identity, lambda: "accepted") == RuntimeActivationCommit(
        admitted=False
    )
    assert registry.is_current(current.identity)


def test_session_binding_lookup_failure_is_not_resource_absence() -> None:
    service = object.__new__(AgentService)
    service.agents = {
        "codex": SimpleNamespace(
            runtime_activation_identity_for_session_binding=lambda **_binding: (
                (_ for _ in ()).throw(RuntimeError("lookup failed"))
            )
        )
    }

    resolution = service.runtime_activation_identity_for_session_binding(
        "codex",
        session_anchor="anchor",
        workdir="/work",
    )

    assert resolution == RuntimeActivationResolution(authoritative=False)


def test_request_lookup_failure_is_not_resource_absence() -> None:
    service = object.__new__(AgentService)
    service.agents = {
        "codex": SimpleNamespace(
            runtime_activation_identity_for_request=lambda _request: (
                (_ for _ in ()).throw(ValueError("ambiguous route"))
            )
        )
    }

    resolution = service.runtime_activation_identity_for_request(
        "codex",
        SimpleNamespace(session_key="route:base"),
    )

    assert resolution == RuntimeActivationResolution(authoritative=False)


def test_claude_request_route_resolution_distinguishes_absence_and_ambiguity() -> None:
    registry = RuntimeActivationRegistry()
    first_identity = registry.attach("claude", "runtime-main")
    second_identity = registry.attach("claude", "runtime-routing")
    claude = object.__new__(ClaudeAgent)
    claude.claude_sessions = {}

    request = SimpleNamespace(session_key="route:base")
    assert claude.runtime_activation_identity_for_request(request) is None

    claude.claude_sessions = {
        "runtime-main": SimpleNamespace(
            _vibe_runtime_activation_identity=first_identity,
            _vibe_runtime_fallback_session_key="route:base",
        )
    }
    assert claude.runtime_activation_identity_for_request(request) == first_identity

    claude.claude_sessions["runtime-routing"] = SimpleNamespace(
        _vibe_runtime_activation_identity=second_identity,
        _vibe_runtime_fallback_session_key="route:base",
    )
    with pytest.raises(ValueError, match="ambiguous"):
        claude.runtime_activation_identity_for_request(request)


def test_hfr_137_claude_ambiguous_route_blocks_fallback_claim() -> None:
    registry = RuntimeActivationRegistry()
    claude = object.__new__(ClaudeAgent)
    claude.claude_sessions = {
        "runtime-main": SimpleNamespace(
            _vibe_runtime_activation_identity=registry.attach(
                "claude", "runtime-main"
            ),
            _vibe_runtime_fallback_session_key="route:base",
        ),
        "runtime-routing": SimpleNamespace(
            _vibe_runtime_activation_identity=registry.attach(
                "claude", "runtime-routing"
            ),
            _vibe_runtime_fallback_session_key="route:base",
        ),
    }
    controller = SimpleNamespace(runtime_activation=registry)
    agent_service = AgentService(controller, activation_registry=registry)
    agent_service.agents = {"claude": claude}
    controller.agent_service = agent_service
    request_store = SimpleNamespace(claim=Mock())
    scheduled = object.__new__(ScheduledTaskService)
    scheduled.controller = controller
    scheduled.request_store = request_store
    pending = TaskExecutionRequest(
        id="run-route",
        request_type="agent_run",
        agent_backend="claude",
        session_key="route:base",
    )

    assert scheduled._claim_pending_request(pending) is None
    request_store.claim.assert_not_called()


def test_backend_without_activation_contract_has_no_runtime_resource() -> None:
    service = object.__new__(AgentService)
    service.agents = {"legacy": SimpleNamespace()}

    resolution = service.runtime_activation_identity_for_request(
        "legacy",
        SimpleNamespace(session_key="route:base"),
    )

    assert resolution == RuntimeActivationResolution(
        authoritative=True,
        identity=None,
    )


def test_fallback_claim_fails_closed_when_runtime_lookup_is_ambiguous() -> None:
    registry = RuntimeActivationRegistry()
    pending = TaskExecutionRequest(
        id="run-ambiguous",
        request_type="agent_run",
        agent_backend="codex",
        session_key="route:base",
    )
    request_store = SimpleNamespace(claim=Mock())
    service = object.__new__(ScheduledTaskService)
    service.controller = SimpleNamespace(
        agent_service=SimpleNamespace(
            agents={"codex": object()},
            activation_registry=registry,
            runtime_activation_identity_for_request=lambda _backend, _request: (
                RuntimeActivationResolution(authoritative=False)
            ),
        ),
        runtime_activation=registry,
    )
    service.request_store = request_store

    assert service._claim_pending_request(pending) is None
    request_store.claim.assert_not_called()


def test_hfr_137_codex_session_key_claim_observes_retired_generation() -> None:
    """HFR-137: legacy routes cannot bypass the exact Codex generation."""

    registry = RuntimeActivationRegistry()
    identity = registry.attach("codex", "/work#1")
    codex = init_generation_state(object.__new__(CodexAgent))
    install_codex_transport(codex, "/work", SimpleNamespace(), activation=identity, sessions={"base": "thread"})
    codex._session_mgr = SimpleNamespace(
        get_sessions_by_session_key=lambda _route: ["base"],
        get_cwd=lambda _base: "/work",
    )
    controller = SimpleNamespace(runtime_activation=registry)
    codex.controller = controller
    agent_service = AgentService(
        controller,
        activation_registry=registry,
    )
    agent_service.agents = {"codex": codex}
    controller.agent_service = agent_service
    request_store = SimpleNamespace(claim=Mock())
    scheduled = object.__new__(ScheduledTaskService)
    scheduled.controller = controller
    scheduled.request_store = request_store
    pending = TaskExecutionRequest(
        id="run-route",
        request_type="agent_run",
        agent_backend="codex",
        session_key="route:base",
    )
    assert registry.retire_if_current(identity, lambda: True)

    assert scheduled._claim_pending_request(pending) is None
    request_store.claim.assert_not_called()


def test_hfr_137_legacy_agent_run_resolves_one_catalog_backend_before_probe() -> None:
    """HFR-137: a legacy Agent reference cannot become a multi-backend probe."""

    registry = RuntimeActivationRegistry()
    codex_identity = registry.attach("codex", "/work")
    registry.attach("opencode", "session-1")
    probes: list[str] = []
    pending = TaskExecutionRequest(
        id="run-custom-agent",
        request_type="agent_run",
        agent_id="agt-custom",
        agent_name="reviewer",
    )

    def resolve(backend: str, _request: Any) -> RuntimeActivationResolution:
        probes.append(backend)
        if backend == "codex":
            return RuntimeActivationResolution(
                authoritative=True,
                identity=codex_identity,
            )
        return RuntimeActivationResolution(
            authoritative=True,
            identity=registry.current("opencode", "session-1"),
        )

    request_store = SimpleNamespace(claim=Mock(return_value=pending))
    scheduled = object.__new__(ScheduledTaskService)
    scheduled.controller = SimpleNamespace(
        vibe_agent_store=SimpleNamespace(
            get_by_id=lambda agent_id: (
                SimpleNamespace(backend="codex")
                if agent_id == "agt-custom"
                else None
            ),
            get=lambda _name: pytest.fail("stable agent_id must win over its name"),
        ),
        agent_service=SimpleNamespace(
            agents={"codex": object(), "opencode": object()},
            activation_registry=registry,
            runtime_activation_identity_for_request=resolve,
        ),
        runtime_activation=registry,
    )
    scheduled.request_store = request_store

    claimed = scheduled._claim_pending_request(pending)

    assert claimed is pending
    assert pending.observed_activation_identity is codex_identity
    assert probes == ["codex"]
    request_store.claim.assert_called_once_with(pending.id)


class _ActivityStore:
    def __init__(self) -> None:
        self.upserts: list[tuple[dict[str, Any], str]] = []

    def upsert_activity(self, activity: dict[str, Any], *, phase: str) -> None:
        self.upserts.append((activity, phase))


def test_activity_registry_remains_compatible_without_activation_registry() -> None:
    store = _ActivityStore()
    activities = SessionActivityRegistry(store)

    started = activities.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-1",
        kind="background_task",
    )
    progressed = activities.progress(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-1",
        description="Halfway",
    )

    assert started is not None
    assert progressed is not None
    assert progressed.description == "Halfway"
    assert [phase for _, phase in store.upserts] == ["active", "active"]


@pytest.mark.parametrize("session_id", [None, "ses-1"])
def test_hfr_137_stale_activity_progress_cannot_rebuild_active_owner(
    session_id: str | None,
) -> None:
    activation_registry = RuntimeActivationRegistry()
    old_identity = activation_registry.attach("claude", "runtime-1")
    assert activation_registry.retire_if_current(old_identity, lambda: True)
    current_identity = activation_registry.attach("claude", "runtime-1")
    store = _ActivityStore()
    activities = SessionActivityRegistry(
        store,
        activation_registry=activation_registry,
    )

    stale = activities.progress(
        backend="claude",
        runtime_key="runtime-1",
        session_id=session_id,
        activity_id="late-task",
        description="Delayed receiver event",
        activation_identity=old_identity,
    )

    assert stale is None
    assert activities.has_active("claude", "runtime-1") is False
    assert store.upserts == []

    current = activities.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id=session_id,
        activity_id="current-task",
        kind="background_task",
        activation_identity=current_identity,
    )
    assert current is not None
    assert activities.has_active("claude", "runtime-1") is True
    assert [phase for _, phase in store.upserts] == ["active"]


def test_hfr_137_stale_activity_completion_and_teardown_preserve_replacement() -> None:
    """HFR-137: only the exact receiver generation can settle its Activity."""

    activation_registry = RuntimeActivationRegistry()
    old_identity = activation_registry.attach("claude", "runtime-1")
    store = _ActivityStore()
    activities = SessionActivityRegistry(
        store,
        activation_registry=activation_registry,
    )
    assert activities.set_connection(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        state="connected",
        activation_identity=old_identity,
    )
    assert activities.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-1",
        kind="background_task",
        activation_identity=old_identity,
    )

    current_identity = activation_registry.attach("claude", "runtime-1")
    assert activities.set_connection(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        state="connected",
        activation_identity=current_identity,
    )
    assert activities.progress(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-1",
        description="Replacement owns progress",
        activation_identity=current_identity,
    )

    stale = activities.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-1",
        status="completed",
        expects_output=True,
        activation_identity=old_identity,
    )
    stale_teardown = activities.end_runtime(
        "claude",
        "runtime-1",
        activation_identity=old_identity,
        retain_terminal_snapshots=True,
    )

    assert stale is None
    assert stale_teardown == []
    assert activities.has_active("claude", "runtime-1") is True
    assert activities.session_state("ses-1")["connection"] == "connected"

    completed = activities.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-1",
        status="completed",
        expects_output=True,
        activation_identity=current_identity,
    )
    assert completed is not None
    assert activities.has_active("claude", "runtime-1") is False


def test_configured_activity_registry_requires_matching_activation_identity() -> None:
    activation_registry = RuntimeActivationRegistry()
    identity = activation_registry.attach("claude", "runtime-1")
    store = _ActivityStore()
    activities = SessionActivityRegistry(
        store,
        activation_registry=activation_registry,
    )

    missing = activities.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id=None,
        activity_id="missing",
        kind="background_task",
    )
    mismatched = activities.start(
        backend="codex",
        runtime_key="runtime-1",
        session_id=None,
        activity_id="mismatched",
        kind="background_task",
        activation_identity=identity,
    )

    assert missing is None
    assert mismatched is None
    assert store.upserts == []


class _NativeStartManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.entered = threading.Event()
        self.allow_commit = threading.Event()

    def on_native_start(
        self,
        _context: Any,
        *,
        backend: str,
        runtime_key: str,
        runtime_turn_id: str,
    ) -> None:
        self.entered.set()
        assert self.allow_commit.wait(timeout=2)
        self.calls.append((backend, runtime_key, runtime_turn_id))


def _native_start_service(
    registry: RuntimeActivationRegistry,
) -> tuple[AgentService, _NativeStartManager, Any]:
    manager = _NativeStartManager()
    controller = SimpleNamespace(session_turns=manager)
    service = AgentService(controller, activation_registry=registry)
    context = SimpleNamespace(
        platform_specific={
            AGENT_RUNTIME_TURN_KEY: "runtime-1",
            AGENT_RUNTIME_TURN_TOKEN: "turn-1",
        }
    )
    gate = service._get_turn_gate("runtime-1")
    gate.token = "turn-1"
    gate.backend = "claude"
    gate.context = context
    return service, manager, context


def test_hfr_137_native_start_admission_first_blocks_retirement_until_durable_commit() -> None:
    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "resource-1")
    service, manager, context = _native_start_service(registry)
    retirement_started = threading.Event()
    retired: list[bool] = []

    admission_thread = threading.Thread(
        target=lambda: service.mark_runtime_turn_started(
            context,
            activation_identity=identity,
        )
    )

    def retire() -> None:
        retirement_started.set()
        retired.append(registry.retire_if_current(identity, lambda: True))

    retirement_thread = threading.Thread(target=retire)
    admission_thread.start()
    assert manager.entered.wait(timeout=2)
    retirement_thread.start()
    assert retirement_started.wait(timeout=2)
    manager.allow_commit.set()

    _join(admission_thread)
    _join(retirement_thread)

    assert manager.calls == [("claude", "runtime-1", "turn-1")]
    assert retired == [True]
    assert service._get_turn_gate("runtime-1").runtime_started is True


def test_hfr_137_native_start_cleanup_first_rejects_old_generation_commit() -> None:
    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "resource-1")
    service, manager, context = _native_start_service(registry)
    predicate_entered = threading.Event()
    allow_retirement = threading.Event()
    admission_started = threading.Event()

    def final_predicate() -> bool:
        predicate_entered.set()
        assert allow_retirement.wait(timeout=2)
        return True

    retirement_thread = threading.Thread(
        target=lambda: registry.retire_if_current(identity, final_predicate)
    )

    def admit() -> None:
        admission_started.set()
        service.mark_runtime_turn_started(context, activation_identity=identity)

    admission_thread = threading.Thread(target=admit)
    retirement_thread.start()
    assert predicate_entered.wait(timeout=2)
    admission_thread.start()
    assert admission_started.wait(timeout=2)
    allow_retirement.set()

    _join(retirement_thread)
    _join(admission_thread)

    assert manager.calls == []
    assert service._get_turn_gate("runtime-1").runtime_started is False


@pytest.mark.parametrize("session_id", [None, "ses-1"])
def test_hfr_137_claude_receiver_forwards_generation_to_activity_admission(
    session_id: str | None,
) -> None:
    activation_registry = RuntimeActivationRegistry()
    old_identity = activation_registry.attach("claude", "runtime-1")
    assert activation_registry.retire_if_current(old_identity, lambda: True)
    store = _ActivityStore()
    activities = SessionActivityRegistry(
        store,
        activation_registry=activation_registry,
    )
    activity_service = SimpleNamespace(activities=activities)
    session_handler = SimpleNamespace(
        mark_session_active=lambda _runtime_key: pytest.fail(
            "stale Activity marked the runtime active"
        ),
        touch_session_activity=lambda _runtime_key: pytest.fail(
            "stale Activity refreshed the runtime baseline"
        ),
    )
    agent = object.__new__(ClaudeAgent)
    agent.controller = SimpleNamespace(agent_service=activity_service)
    agent.session_handler = session_handler
    agent._pending_requests = {}
    agent._detached_foreground_tool_use_ids = {}
    agent._detached_foreground_task_ids = {}
    agent._foreground_tool_use_ids = {}
    context = SimpleNamespace(
        platform_specific={"agent_session_id": session_id} if session_id else {}
    )
    message = SimpleNamespace(
        subtype="task_started",
        task_id="late-task",
        task_type="background_task",
        description="Delayed receiver event",
    )

    handled = agent._handle_activity_message(
        message,
        "runtime-1",
        context,
        activation_identity=old_identity,
    )

    assert handled is True
    assert activities.has_active("claude", "runtime-1") is False
    assert store.upserts == []


def _fallback_service(
    registry: RuntimeActivationRegistry,
    request_store: Any,
    identity: Any,
    *,
    backend_ready: bool = True,
) -> ScheduledTaskService:
    agent_service = SimpleNamespace(
        activation_registry=registry,
        agents={"claude": object()},
        is_backend_ready=lambda _backend: backend_ready,
        runtime_activation_identity_for_request=lambda backend, request: (
            identity if backend == "claude" else None
        ),
    )
    service = object.__new__(ScheduledTaskService)
    service.controller = SimpleNamespace(
        agent_service=agent_service,
        runtime_activation=registry,
    )
    service.request_store = request_store
    return service


def test_hfr_137_cleanup_first_rejects_fallback_claim() -> None:
    """HFR-137: a retired generation cannot turn its queued Run into a claim."""

    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    predicate_entered = threading.Event()
    allow_retirement = threading.Event()
    admission_started = threading.Event()
    claim_calls: list[str] = []
    claims: list[TaskExecutionRequest | None] = []
    pending = TaskExecutionRequest(
        id="run-1",
        request_type="agent_run",
        agent_backend="claude",
    )
    request_store = SimpleNamespace(
        claim=lambda request_id: claim_calls.append(request_id) or pending,
    )
    service = _fallback_service(registry, request_store, identity)

    def retire() -> None:
        def final_predicate() -> bool:
            predicate_entered.set()
            assert allow_retirement.wait(timeout=2)
            return True

        assert registry.retire_if_current(identity, final_predicate)

    def claim() -> None:
        admission_started.set()
        claims.append(service._claim_pending_request(pending))

    retirement_thread = threading.Thread(target=retire)
    claim_thread = threading.Thread(target=claim)
    retirement_thread.start()
    assert predicate_entered.wait(timeout=2)
    claim_thread.start()
    assert admission_started.wait(timeout=2)
    allow_retirement.set()

    _join(retirement_thread)
    _join(claim_thread)

    assert claims == [None]
    assert claim_calls == []


def test_hfr_137_claim_that_loses_to_cleanup_during_its_write_is_given_back() -> None:
    """HFR-137, #2655: a claim writes with no boundary held, so a cleanup that starts meanwhile retires the generation
    at once instead of waiting for the write. The claim, confirmed after it lands, finds its generation retired and is
    given back to the queue, so nothing ever starts on the retired generation."""

    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    claim_entered = threading.Event()
    allow_claim = threading.Event()
    pending = TaskExecutionRequest(
        id="run-1",
        request_type="agent_run",
        agent_backend="claude",
    )
    claimed_request = TaskExecutionRequest(
        id=pending.id,
        request_type=pending.request_type,
        agent_backend=pending.agent_backend,
    )
    requeued: list[str] = []

    def claim(_request_id: str) -> TaskExecutionRequest:
        claim_entered.set()
        assert allow_claim.wait(timeout=2)
        return claimed_request

    request_store = SimpleNamespace(
        claim=claim,
        requeue=lambda request_id: requeued.append(request_id),
    )
    service = _fallback_service(registry, request_store, identity)
    claims: list[TaskExecutionRequest | None] = []
    retired: list[bool] = []

    claim_thread = threading.Thread(
        target=lambda: claims.append(service._claim_pending_request(pending))
    )
    retirement_thread = threading.Thread(
        target=lambda: retired.append(registry.retire_if_current(identity, lambda: True))
    )
    claim_thread.start()
    assert claim_entered.wait(timeout=2)
    retirement_thread.start()
    # The cleanup does not wait for the claim's write.
    _join(retirement_thread)
    assert retired == [True]
    allow_claim.set()
    _join(claim_thread)

    assert claims == [None]
    assert requeued == [pending.id]


def test_hfr_137_backend_drain_rejects_fallback_claim() -> None:
    """HFR-137: a draining backend cannot acquire a fallback Run claim."""

    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    pending = TaskExecutionRequest(
        id="run-1",
        request_type="agent_run",
        agent_backend="claude",
    )
    claim_calls: list[str] = []
    request_store = SimpleNamespace(
        claim=lambda request_id: claim_calls.append(request_id) or pending,
    )
    service = _fallback_service(
        registry,
        request_store,
        identity,
        backend_ready=False,
    )

    assert service._claim_pending_request(pending) is None
    assert claim_calls == []


def test_hfr_137_backend_drain_requeues_existing_pre_pid_claim() -> None:
    """HFR-137: a pre-PID claim never waits on the drain it would pin."""

    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    requeued: list[str] = []
    request = TaskExecutionRequest(
        id="run-1",
        request_type="agent_run",
        agent_backend="claude",
        observed_activation_identity=identity,
    )
    request_store = SimpleNamespace(
        refresh_claimed_request=lambda current: current,
        mark_execution_started=lambda _request_id: pytest.fail("draining claim started"),
        requeue=lambda request_id: requeued.append(request_id),
    )
    service = _fallback_service(
        registry,
        request_store,
        identity,
        backend_ready=False,
    )

    marked_request, started = asyncio.run(
        service._mark_execution_started_for_claimed_request(request)
    )

    assert marked_request is request
    assert started is False
    assert requeued == [request.id]


def test_hfr_137_pid_marker_that_loses_to_cleanup_during_its_write_is_requeued() -> None:
    """HFR-137, #2655: the start marker writes with no boundary held, so a cleanup that starts meanwhile retires the
    generation at once instead of waiting for the write. The start, confirmed after the marker lands, finds its
    generation retired: the execution does not start, and the claim goes back to the queue, marker cleared."""

    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    marker_entered = threading.Event()
    allow_marker = threading.Event()
    request = TaskExecutionRequest(
        id="run-1",
        request_type="agent_run",
        agent_backend="claude",
        observed_activation_identity=identity,
    )
    requeued: list[str] = []

    def mark_execution_started(_request_id: str) -> bool:
        marker_entered.set()
        assert allow_marker.wait(timeout=2)
        return True

    request_store = SimpleNamespace(
        refresh_claimed_request=lambda current: current,
        mark_execution_started=mark_execution_started,
        requeue=lambda request_id: requeued.append(request_id),
    )
    service = _fallback_service(registry, request_store, identity)
    marked: list[tuple[TaskExecutionRequest, bool]] = []
    retired: list[bool] = []

    marker_thread = threading.Thread(
        target=lambda: marked.append(
            asyncio.run(service._mark_execution_started_for_claimed_request(request))
        )
    )
    retirement_thread = threading.Thread(
        target=lambda: retired.append(registry.retire_if_current(identity, lambda: True))
    )
    marker_thread.start()
    assert marker_entered.wait(timeout=2)
    retirement_thread.start()
    # The cleanup does not wait for the marker's write.
    _join(retirement_thread)
    assert retired == [True]
    allow_marker.set()
    _join(marker_thread)

    assert marked == [(request, False)]
    assert requeued == [request.id]


def test_a_claim_waiting_for_the_sqlite_writer_never_holds_the_boundary_the_loop_needs(tmp_path) -> None:
    """#2655: the scheduler claims off the loop, and its claim waits for SQLite's writer. The loop admits Session Turns
    inside a transaction that already holds the writer, and only then takes the activation boundary. A claim that held
    the boundary while it waited for the writer would make the loop wait for SQLite's busy timeout. It never holds it
    there, so the loop takes the boundary at once, and the claim lands when the loop's transaction ends."""

    db_path = tmp_path / "state.sqlite"
    with closing(sqlite3.connect(db_path)) as setup:
        setup.execute("CREATE TABLE claims (run_id TEXT)")
        setup.commit()
    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    pending = TaskExecutionRequest(id="run-1", request_type="agent_run", agent_backend="claude")
    claim_writing = threading.Event()

    def claim(request_id: str) -> TaskExecutionRequest:
        with closing(sqlite3.connect(db_path, timeout=2)) as conn:
            claim_writing.set()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO claims VALUES (?)", (request_id,))
            conn.commit()
        return pending

    request_store = SimpleNamespace(
        claim=claim,
        requeue=lambda _request_id: pytest.fail("a claim made on a live generation was given back"),
    )
    service = _fallback_service(registry, request_store, identity)
    claims: list[TaskExecutionRequest | None] = []
    errors: list[BaseException] = []

    def run_claim() -> None:
        try:
            claims.append(service._claim_pending_request(pending))
        except Exception as exc:
            errors.append(exc)

    with closing(sqlite3.connect(db_path, timeout=2)) as loop_transaction:
        loop_transaction.execute("BEGIN IMMEDIATE")
        claim_thread = threading.Thread(target=run_claim)
        claim_thread.start()
        assert claim_writing.wait(timeout=2)
        time.sleep(0.1)  # the claim now waits for the writer this transaction holds
        started = time.monotonic()
        with registry.hold_if_current(identity) as admitted:
            waited = time.monotonic() - started
        loop_transaction.commit()
    _join(claim_thread)

    assert waited < 0.05, f"the loop waited {waited:.2f}s on a claim that held the boundary while waiting for SQLite"
    assert admitted is True
    assert errors == []
    assert claims == [pending]


def test_a_claim_confirmed_before_a_graceful_cleanup_keeps_its_generation() -> None:
    """#2655: a claim confirmed while its generation was live stands. A graceful cleanup whose final snapshot sees the
    claimed Run as an owner declines, and the Run then starts on that generation."""

    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    pending = TaskExecutionRequest(id="run-1", request_type="agent_run", agent_backend="claude")
    owners: set[str] = set()
    request_store = SimpleNamespace(
        claim=lambda request_id: owners.add(request_id) or pending,
        refresh_claimed_request=lambda current: current,
        mark_execution_started=lambda _request_id: True,
        requeue=lambda _request_id: pytest.fail("a confirmed claim was given back"),
    )
    service = _fallback_service(registry, request_store, identity)

    claimed = service._claim_pending_request(pending)

    assert claimed is pending
    assert registry.retire_if_current(identity, lambda: not owners) is False
    assert asyncio.run(service._mark_execution_started_for_claimed_request(claimed)) == (claimed, True)


def _sqlite_request_store(tmp_path, monkeypatch) -> tuple[TaskExecutionStore, TaskExecutionRequest]:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    queued = request_store.enqueue_agent_run(message="run", agent_backend="claude", session_id="ses-runtime")
    return request_store, queued


def _failing_requeue(_request_id: str) -> None:
    raise sqlite3.OperationalError("database is locked")


def test_a_claim_whose_give_back_fails_starts_nothing_and_is_requeued_by_recovery(
    tmp_path, monkeypatch, caplog
) -> None:
    """#2655: a claim that lost to a cleanup is given back to the queue. When that write fails too, the claim is still
    refused, so nothing starts on the retired generation, and the failure is logged. The claim it left has no start
    marker, so the claimed-run recovery puts the Run back in the queue, to run again."""

    request_store, queued = _sqlite_request_store(tmp_path, monkeypatch)
    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")

    def claim_while_cleanup_lands(request_id: str) -> TaskExecutionRequest | None:
        assert registry.retire_if_current(identity, lambda: True)
        return request_store.claim(request_id)

    service = _fallback_service(
        registry,
        SimpleNamespace(claim=claim_while_cleanup_lands, requeue=_failing_requeue),
        identity,
    )

    with caplog.at_level(logging.ERROR, logger="core.runtime_activation"):
        assert service._claim_pending_request(queued) is None
    assert "lost to the retirement of claude" in caplog.text
    left = request_store.get_run(queued.id)
    assert left["status"] == "running" and left["pid"] is None

    recovery = FallbackRequestRecoveryHandler(request_store.sqlite_backend)
    items, _has_more = recovery.scan(limit=10, occupied=frozenset())
    assert [recovery.process_sync(item) for item in items] == [True]
    assert request_store.get_run(queued.id)["status"] == "queued"


def test_a_start_whose_give_back_fails_never_runs_and_settles_as_interrupted(tmp_path, monkeypatch) -> None:
    """#2655: a start marker that lost to a cleanup is cleared by giving the claim back. When that write fails too, the
    execution never starts. The marker it left reads, to recovery, as an execution that may have begun, so recovery
    settles the Run as interrupted, with its failure notice, rather than running it again: the cost of a failed write
    racing a retirement, which only a persisted generation could undo (see the design note)."""

    request_store, queued = _sqlite_request_store(tmp_path, monkeypatch)
    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    claimed = request_store.claim(queued.id)
    assert claimed is not None
    claimed.observed_activation_identity = identity

    def mark_while_cleanup_lands(request_id: str) -> bool:
        assert registry.retire_if_current(identity, lambda: True)
        return request_store.mark_execution_started(request_id)

    service = _fallback_service(
        registry,
        SimpleNamespace(
            refresh_claimed_request=lambda current: current,
            mark_execution_started=mark_while_cleanup_lands,
            requeue=_failing_requeue,
        ),
        identity,
    )

    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(service._mark_execution_started_for_claimed_request(claimed))
    assert request_store.get_run(queued.id)["pid"] is not None

    request_store.recover_processing()
    settled = request_store.get_run(queued.id)
    assert settled["status"] == "failed" and settled["pid"] is None


def test_a_boundary_held_off_the_loop_thread_is_reported_with_its_caller(caplog) -> None:
    """#2655: off the controller loop thread, ``commit_if_current`` and ``hold_if_current`` would hold the boundary
    across their commit's I/O while the loop takes it synchronously. Such a call is logged as an error naming its
    caller, so a new off-loop holder cannot bring the loop stall back unnoticed."""

    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    registry.bind_loop_thread()  # this thread plays the controller loop

    def holder_on_a_worker_commits() -> None:
        registry.commit_if_current(identity, lambda: None)

    def holder_on_a_worker_holds() -> None:
        with registry.hold_if_current(identity):
            pass

    with caplog.at_level(logging.ERROR, logger="core.runtime_activation"):
        registry.commit_if_current(identity, lambda: None)
        with registry.hold_if_current(identity):
            pass
        assert not caplog.records, "a holder on the loop thread was reported"
        for target in (holder_on_a_worker_commits, holder_on_a_worker_holds):
            worker = threading.Thread(target=target)
            worker.start()
            _join(worker)

    reports = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert len(reports) == 2
    assert "commit_if_current" in reports[0] and "in holder_on_a_worker_commits" in reports[0]
    assert "hold_if_current" in reports[1] and "in holder_on_a_worker_holds" in reports[1]


