from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import paths
from config.v2_config import AgentsConfig, PlatformsConfig, RemoteAccessConfig, RuntimeConfig, SlackConfig, UiConfig, V2Config
from storage import resource_access_service
from storage.db import get_cached_sqlite_engine
from storage.migrations import run_migrations
from vibe import remote_access


@dataclass
class _Response:
    payload: dict[str, Any]
    status_code: int = 200

    def json(self) -> dict[str, Any]:
        return self.payload


def _config() -> V2Config:
    config = V2Config(
        mode="self_host",
        version="v2",
        platform="slack",
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        remote_access=RemoteAccessConfig(),
    )
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.backend_url = "https://backend.test"
    cloud.instance_id = "inst-1"
    cloud.instance_secret = "paired-device-secret"
    return config


def _seed_policy() -> None:
    paths.ensure_data_dirs()
    run_migrations()
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="agent",
            resource_id="agent-1",
            organization_id="org-1",
            owner_user_id="owner-1",
            access_level="private",
            policy_revision=1,
            last_applied_control_plane_revision=1,
        )


def _descriptor() -> dict[str, Any]:
    return {
        "resource_kind": "agent",
        "resource_id": "agent-1",
        "display_name": "Research agent",
        "owner_user_id": "owner-1",
        "metadata_revision": 1,
        "applied_acl_revision": 1,
        "access_level": "private",
        "group_ids": [],
    }


def test_sync_applies_only_newer_intent_and_acknowledges_exact_revision(monkeypatch) -> None:
    _seed_policy()
    config = _config()
    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            _Response({"organization_id": "org-1", "resources": []}),
            _Response(
                {
                    "organization_id": "org-1",
                    "poll_after_seconds": 30,
                    "intents": [
                        {
                            "resource_kind": "agent",
                            "resource_id": "agent-1",
                            "revision": 2,
                            "access_level": "scope",
                            "group_ids": ["group-engineering"],
                        }
                    ],
                }
            ),
            _Response({"resource": {"resource_kind": "agent", "resource_id": "agent-1"}}),
            _Response({"organization_id": "org-1", "resources": []}),
            _Response(
                {
                    "organization_id": "org-1",
                    "poll_after_seconds": 30,
                    "intents": [
                        {
                            "resource_kind": "agent",
                            "resource_id": "agent-1",
                            "revision": 1,
                            "access_level": "private",
                            "group_ids": [],
                        }
                    ],
                }
            ),
        ]
    )

    def request(method: str, url: str, **kwargs: Any) -> _Response:
        calls.append({"method": method, "url": url, **kwargs})
        return next(responses)

    monkeypatch.setattr(remote_access.requests, "request", request)

    first = remote_access.sync_resource_acl_once(
        config,
        organization_id="org-1",
        resources=[_descriptor()],
    )
    second = remote_access.sync_resource_acl_once(
        config,
        organization_id="org-1",
        resources=[_descriptor()],
    )

    assert first["ok"] is True
    assert first["organizations"][0]["applied"] == 1
    assert second["ok"] is True
    assert second["organizations"][0]["skipped"] == 1
    assert [call["method"] for call in calls] == ["PUT", "GET", "POST", "PUT", "GET"]
    assert calls[0]["headers"]["X-Vibe-Device-Secret"] == "paired-device-secret"
    assert set(calls[0]["json"]["resources"][0]) <= {
        "resource_id",
        "resource_kind",
        "display_name",
        "owner_user_id",
        "metadata_revision",
        "applied_acl_revision",
        "access_level",
        "group_ids",
        "sync_status",
    }
    assert calls[2]["json"] == {
        "resource_kind": "agent",
        "resource_id": "agent-1",
        "revision": 2,
        "outcome": "applied",
    }

    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        policy = resource_access_service.get_resource_policy("agent", "agent-1", connection=connection)
    assert policy is not None
    assert policy["access_level"] == "scope"
    assert policy["group_ids"] == ["group-engineering"]
    assert policy["last_applied_control_plane_revision"] == 2


def test_sync_offline_retains_last_applied_policy(monkeypatch) -> None:
    _seed_policy()
    config = _config()

    def request(*_args: Any, **_kwargs: Any):
        raise remote_access.requests.ConnectionError("offline")

    monkeypatch.setattr(remote_access.requests, "request", request)

    result = remote_access.sync_resource_acl_once(
        config,
        organization_id="org-1",
        resources=[_descriptor()],
    )

    assert result["ok"] is False
    assert result["organizations"][0]["error"] == "resource_acl_sync_failed"
    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        policy = resource_access_service.get_resource_policy("agent", "agent-1", connection=connection)
    assert policy is not None
    assert policy["access_level"] == "private"
    assert policy["last_applied_control_plane_revision"] == 1
