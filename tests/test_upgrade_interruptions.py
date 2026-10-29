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
        count_runs_by_status=lambda *, run_type: (
            {"queued": 1, "running": 2}
            if run_type == "watch"
            else {"queued": 3, "running": 4}
        ),
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
async def test_upgrade_interruptions_fail_closed_when_controller_is_unavailable(
    monkeypatch,
) -> None:
    async def unavailable():
        raise internal_client.InternalServerUnavailable("down")

    store = SimpleNamespace(
        count_runs_by_status=lambda *, run_type: {
            "queued": 0,
            "running": 0,
        },
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
