from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from core.runtime_activation import RuntimeActivationCommit, RuntimeActivationRegistry
from core.session_activities import SessionActivityRegistry
from modules.agents.base import AGENT_RUNTIME_TURN_KEY, AGENT_RUNTIME_TURN_TOKEN
from modules.agents.claude_agent import ClaudeAgent
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
def test_stale_activity_progress_cannot_rebuild_active_owner(
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


def test_native_start_admission_first_blocks_retirement_until_durable_commit() -> None:
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


def test_native_start_cleanup_first_rejects_old_generation_commit() -> None:
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
def test_claude_receiver_forwards_generation_to_activity_admission(
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
