from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from core.runtime_activation import (
    RuntimeActivationCommit,
    RuntimeActivationRegistry,
    RuntimeActivationResolution,
)
from core.scheduled_tasks import ScheduledTaskService, TaskExecutionRequest
from core.session_activities import SessionActivityRegistry
from modules.agents.base import AGENT_RUNTIME_TURN_KEY, AGENT_RUNTIME_TURN_TOKEN
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.codex.agent import CodexAgent
from modules.agents.opencode.agent import OpenCodeAgent
from modules.agents.service import AgentService


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

    codex_identity = registry.attach("codex", "/work")
    codex_transport = SimpleNamespace(
        _vibe_runtime_activation_identity=codex_identity,
    )
    codex = object.__new__(CodexAgent)
    codex._transports = {"/work": codex_transport}
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


def test_opencode_binding_resolver_returns_pre_prompt_shared_generation() -> None:
    registry = RuntimeActivationRegistry()

    class _Server:
        base_url = "http://127.0.0.1:4096"

        def set_runtime_activation_retire(self, callback) -> None:
            self.retire_activation = callback

    server = _Server()
    opencode = object.__new__(OpenCodeAgent)
    opencode.controller = SimpleNamespace(runtime_activation=registry)
    opencode._client_manager = SimpleNamespace(_server_manager=server)
    opencode._session_manager = SimpleNamespace(
        get_request_session=lambda anchor: (
            ("", "/work", "route") if anchor == "anchor" else None
        )
    )

    identity = opencode._attach_server_activation(server)

    assert identity is not None
    assert opencode.runtime_activation_identity_for_request(SimpleNamespace()) == identity
    assert opencode.runtime_activation_identity_for_session_binding(
        session_anchor="anchor",
        workdir="/work",
    ) == identity
    with pytest.raises(ValueError, match="changed workdir"):
        opencode.runtime_activation_identity_for_session_binding(
            session_anchor="anchor",
            workdir="/other",
        )

    assert server.retire_activation(True, False)
    replacement = opencode.runtime_activation_identity_for_request(SimpleNamespace())
    late_prompt_commit = registry.commit_if_current(identity, lambda: "accepted")

    assert replacement is not None and replacement != identity
    assert late_prompt_commit == RuntimeActivationCommit(admitted=False)
    assert registry.is_current(replacement)


def test_mh_runtime_007_opencode_overlay_restart_retires_pre_native_start_owner() -> None:
    """MH-RUNTIME-007: a claimed pre-native Turn cannot block its overlay change."""

    registry = RuntimeActivationRegistry()

    class _Server:
        base_url = "http://127.0.0.1:4096"

        def set_runtime_activation_retire(self, callback) -> None:
            self.retire_activation = callback

        def runtime_has_active_turns(self) -> bool:
            return False

    server = _Server()
    opencode = object.__new__(OpenCodeAgent)
    opencode.controller = SimpleNamespace(runtime_activation=registry)
    opencode.runtime_ownership_snapshots = lambda: (
        SimpleNamespace(
            blocks_reclamation=True,
            blocks_transport_replacement=False,
            blocks_transport_replacement_after_turn_drain=False,
            has_active_turn_evidence=False,
        ),
    )

    identity = opencode._attach_server_activation(server)

    assert identity is not None
    assert server.retire_activation(False, False)
    assert not registry.is_current(identity)


def test_opencode_overlay_restart_preserves_native_active_owner() -> None:
    registry = RuntimeActivationRegistry()

    class _Server:
        base_url = "http://127.0.0.1:4096"

        def set_runtime_activation_retire(self, callback) -> None:
            self.retire_activation = callback

        def runtime_has_active_turns(self) -> bool:
            return True

    server = _Server()
    opencode = object.__new__(OpenCodeAgent)
    opencode.controller = SimpleNamespace(runtime_activation=registry)
    opencode.runtime_ownership_snapshots = lambda: (
        SimpleNamespace(
            blocks_reclamation=True,
            blocks_transport_replacement=True,
            blocks_transport_replacement_after_turn_drain=False,
            has_active_turn_evidence=True,
        ),
    )

    identity = opencode._attach_server_activation(server)

    assert identity is not None
    assert not server.retire_activation(False, False)
    assert registry.is_current(identity)


def test_mh_runtime_007_opencode_overlay_restart_ignores_drained_durable_active_turn() -> None:
    """MH-RUNTIME-007: a stale durable Turn cannot outvote the server drain."""

    registry = RuntimeActivationRegistry()

    class _Server:
        base_url = "http://127.0.0.1:4096"

        def set_runtime_activation_retire(self, callback) -> None:
            self.retire_activation = callback

        def runtime_has_active_turns(self) -> bool:
            return True

    server = _Server()
    opencode = object.__new__(OpenCodeAgent)
    opencode.controller = SimpleNamespace(runtime_activation=registry)
    opencode.runtime_ownership_snapshots = lambda: (
        SimpleNamespace(
            blocks_reclamation=True,
            blocks_transport_replacement=True,
            blocks_transport_replacement_after_turn_drain=False,
            has_active_turn_evidence=True,
        ),
    )

    identity = opencode._attach_server_activation(server)

    assert identity is not None
    assert server.retire_activation(False, True)
    assert not registry.is_current(identity)


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
    identity = registry.attach("codex", "/work")
    transport = SimpleNamespace(_vibe_runtime_activation_identity=identity)
    codex = object.__new__(CodexAgent)
    codex._transports = {"/work": transport}
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


def test_hfr_137_claim_first_requeues_when_cleanup_wins_before_pid() -> None:
    """HFR-137: a pre-PID claim survives cleanup through exact Run requeue."""

    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    claim_entered = threading.Event()
    allow_claim = threading.Event()
    retirement_started = threading.Event()
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
    mark_calls: list[str] = []
    requeued: list[str] = []

    def claim(_request_id: str) -> TaskExecutionRequest:
        claim_entered.set()
        assert allow_claim.wait(timeout=2)
        return claimed_request

    request_store = SimpleNamespace(
        claim=claim,
        refresh_claimed_request=lambda request: request,
        mark_execution_started=lambda request_id: mark_calls.append(request_id) or True,
        requeue=lambda request_id: requeued.append(request_id),
    )
    service = _fallback_service(registry, request_store, identity)
    claims: list[TaskExecutionRequest | None] = []
    retired: list[bool] = []

    claim_thread = threading.Thread(
        target=lambda: claims.append(service._claim_pending_request(pending))
    )

    def retire() -> None:
        retirement_started.set()
        retired.append(registry.retire_if_current(identity, lambda: True))

    retirement_thread = threading.Thread(target=retire)
    claim_thread.start()
    assert claim_entered.wait(timeout=2)
    retirement_thread.start()
    assert retirement_started.wait(timeout=2)
    allow_claim.set()

    _join(claim_thread)
    _join(retirement_thread)

    assert claims == [claimed_request]
    assert claimed_request.observed_activation_identity == identity
    assert retired == [True]

    request, started = asyncio.run(
        service._mark_execution_started_for_claimed_request(claimed_request)
    )

    assert request is claimed_request
    assert started is False
    assert mark_calls == []
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


def test_hfr_137_fallback_pid_marker_first_blocks_retirement_until_commit() -> None:
    """HFR-137: PID admission completes before a competing cleanup can retire."""

    registry = RuntimeActivationRegistry()
    identity = registry.attach("claude", "runtime-1")
    marker_entered = threading.Event()
    allow_marker = threading.Event()
    retirement_started = threading.Event()
    order: list[str] = []
    request = TaskExecutionRequest(
        id="run-1",
        request_type="agent_run",
        agent_backend="claude",
        observed_activation_identity=identity,
    )

    def mark_execution_started(_request_id: str) -> bool:
        order.append("marker-entered")
        marker_entered.set()
        assert allow_marker.wait(timeout=2)
        order.append("marker-finished")
        return True

    request_store = SimpleNamespace(
        refresh_claimed_request=lambda current: current,
        mark_execution_started=mark_execution_started,
        requeue=lambda _request_id: pytest.fail("admitted marker was requeued"),
    )
    service = _fallback_service(registry, request_store, identity)
    marked: list[tuple[TaskExecutionRequest, bool]] = []
    retired: list[bool] = []

    marker_thread = threading.Thread(
        target=lambda: marked.append(
            asyncio.run(service._mark_execution_started_for_claimed_request(request))
        )
    )

    def retire() -> None:
        retirement_started.set()

        def final_predicate() -> bool:
            order.append("retire-predicate")
            return True

        retired.append(registry.retire_if_current(identity, final_predicate))

    retirement_thread = threading.Thread(target=retire)
    marker_thread.start()
    assert marker_entered.wait(timeout=2)
    retirement_thread.start()
    assert retirement_started.wait(timeout=2)
    allow_marker.set()

    _join(marker_thread)
    _join(retirement_thread)

    assert marked == [(request, True)]
    assert retired == [True]
    assert order == ["marker-entered", "marker-finished", "retire-predicate"]
