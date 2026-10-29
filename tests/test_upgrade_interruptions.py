import threading
from types import SimpleNamespace

import pytest

from vibe import internal_client
from vibe.upgrade_interruptions import count_upgrade_interruptions


@pytest.mark.asyncio
async def test_upgrade_interruptions_count_active_agents_watches_and_tasks(
    monkeypatch,
) -> None:
    async def running_agents():
        return {
            "body": {
                "agents": [
                    {"state": "active"},
                    {"state": "active"},
                    {"state": "idle"},
                ]
            }
        }

    store = SimpleNamespace(
        count_runs=lambda *, status, run_type: {
            ("watch", "queued"): 1,
            ("watch", "running"): 2,
            ("scheduled", "queued"): 3,
            ("scheduled", "running"): 4,
        }[(run_type, status)],
        close=lambda: None,
    )
    monkeypatch.setattr(internal_client, "list_running_agents", running_agents)
    monkeypatch.setattr(
        "storage.background.SQLiteBackgroundTaskStore",
        lambda **_kwargs: store,
    )

    result = await count_upgrade_interruptions()
    assert result.as_dict() == {
        "agents": 2,
        "watches": 3,
        "tasks": 7,
        "total": 12,
        "unknown": False,
    }
    assert result.requires_confirmation is True


@pytest.mark.asyncio
async def test_upgrade_interruptions_query_harness_rows_off_the_event_loop(
    monkeypatch,
) -> None:
    async def running_agents():
        return {"body": {"agents": []}}

    event_loop_thread = threading.get_ident()
    calls: list[tuple[int, str, str]] = []
    closed_on: list[int] = []

    class Store:
        def __init__(self, **_kwargs) -> None:
            calls.append((threading.get_ident(), "construct", ""))

        def count_runs(self, *, status: str, run_type: str) -> int:
            calls.append((threading.get_ident(), run_type, status))
            return 1

        def close(self) -> None:
            closed_on.append(threading.get_ident())

    monkeypatch.setattr(internal_client, "list_running_agents", running_agents)
    monkeypatch.setattr("storage.background.SQLiteBackgroundTaskStore", Store)

    result = await count_upgrade_interruptions()

    assert result.watches == 2
    assert result.tasks == 2
    assert [(run_type, status) for _, run_type, status in calls[1:]] == [
        ("watch", "queued"),
        ("watch", "running"),
        ("scheduled", "queued"),
        ("scheduled", "running"),
    ]
    assert all(thread_id != event_loop_thread for thread_id, _, _ in calls)
    assert closed_on and closed_on[0] != event_loop_thread


@pytest.mark.asyncio
async def test_upgrade_interruptions_fail_closed_when_controller_is_unavailable(
    monkeypatch,
) -> None:
    async def unavailable():
        raise internal_client.InternalServerUnavailable("down")

    store = SimpleNamespace(
        count_runs=lambda *, status, run_type: 0,
        close=lambda: None,
    )
    monkeypatch.setattr(internal_client, "list_running_agents", unavailable)
    monkeypatch.setattr(
        "storage.background.SQLiteBackgroundTaskStore",
        lambda **_kwargs: store,
    )

    result = await count_upgrade_interruptions()
    assert result.total == 0
    assert result.unknown is True
    assert result.requires_confirmation is True
