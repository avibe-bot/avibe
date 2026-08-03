from __future__ import annotations

import threading
from typing import Any

import pytest

from core.runtime_activation import RuntimeActivationCommit, RuntimeActivationRegistry
from core.session_activities import SessionActivityRegistry


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
