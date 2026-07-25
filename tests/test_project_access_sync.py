from __future__ import annotations

from dataclasses import dataclass

import requests

from config.v2_config import (
    AgentsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)
from storage import project_access_service, projects_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from vibe import project_access_sync


@dataclass
class _Response:
    payload: dict
    status_code: int = 200

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self.payload


def _config() -> V2Config:
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        remote_access=RemoteAccessConfig(),
    )
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.backend_url = "https://control.example"
    cloud.instance_id = "inst_123"
    cloud.instance_secret = "device-secret"
    return config


def _create_project(tmp_path, name: str = "Project One"):
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    folder = tmp_path / name.replace(" ", "-").lower()
    folder.mkdir()
    with engine.begin() as conn:
        project = projects_service.create_project(conn, str(folder), display_name=name)
    return engine, project


def test_project_index_descriptors_exclude_local_content(tmp_path) -> None:
    engine, active = _create_project(tmp_path)
    archived_folder = tmp_path / "archived"
    archived_folder.mkdir()
    with engine.begin() as conn:
        archived = projects_service.create_project(
            conn,
            str(archived_folder),
            display_name="Archived Project",
        )
        project_access_service.apply_project_access_intent(
            conn,
            {
                "project_id": active["id"],
                "revision": 4,
                "mode": "inherit",
                "bindings": [],
            },
        )
        projects_service.archive_project(conn, archived["id"])

    descriptors = project_access_sync.project_index_descriptors()
    by_id = {descriptor["project_id"]: descriptor for descriptor in descriptors}

    assert set(by_id[active["id"]]) == {
        "project_id",
        "display_name",
        "metadata_revision",
        "applied_access_revision",
        "sync_status",
    }
    assert by_id[active["id"]]["applied_access_revision"] == 4
    assert by_id[active["id"]]["sync_status"] == "in_sync"
    assert by_id[archived["id"]]["sync_status"] == "deleted"
    serialized = repr(descriptors)
    assert str(tmp_path) not in serialized
    assert "folder_path" not in serialized


def test_sync_publishes_applies_and_acks_exact_revision(monkeypatch, tmp_path) -> None:
    engine, project = _create_project(tmp_path)
    calls: list[tuple[str, str, dict | None]] = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        assert kwargs["headers"]["X-Vibe-Device-Secret"] == "device-secret"
        if url.endswith("/project-index"):
            return _Response({"poll_after_seconds": 30, "projects": []})
        if url.endswith("/project-access-intents"):
            return _Response(
                {
                    "poll_after_seconds": 45,
                    "intents": [
                        {
                            "project_id": project["id"],
                            "revision": 2,
                            "mode": "restricted",
                            "bindings": [
                                {
                                    "principal_kind": "email",
                                    "principal_value": "member@example.com",
                                    "access_role": "editor",
                                }
                            ],
                        }
                    ],
                }
            )
        return _Response({"project": {}})

    monkeypatch.setattr(project_access_sync.requests, "request", request)
    result = project_access_sync.sync_project_access_once(_config())

    assert result == {
        "ok": True,
        "configured": True,
        "projects": 1,
        "intents": 1,
        "applied": 1,
        "rejected": 0,
        "stale": 0,
        "ack_errors": 0,
        "poll_after_seconds": 45,
    }
    assert calls[-1] == (
        "POST",
        f"https://control.example/api/v1/instances/inst_123/project-access-acks",
        {"project_id": project["id"], "revision": 2, "outcome": "applied"},
    )
    with engine.connect() as conn:
        policy = project_access_service.get_project_policy(conn, project["id"])
    assert policy is not None
    assert policy["last_applied_control_plane_revision"] == 2


def test_lost_ack_retries_without_reapplying_policy(monkeypatch, tmp_path) -> None:
    engine, project = _create_project(tmp_path)
    ack_attempts = 0

    def request(method, url, **kwargs):
        nonlocal ack_attempts
        if url.endswith("/project-index"):
            return _Response({"poll_after_seconds": 30, "projects": []})
        if url.endswith("/project-access-intents"):
            return _Response(
                {
                    "poll_after_seconds": 30,
                    "intents": [
                        {
                            "project_id": project["id"],
                            "revision": 1,
                            "mode": "restricted",
                            "bindings": [],
                        }
                    ],
                }
            )
        ack_attempts += 1
        if ack_attempts == 1:
            raise requests.ConnectionError("lost response")
        return _Response({"project": {}})

    monkeypatch.setattr(project_access_sync.requests, "request", request)

    first = project_access_sync.sync_project_access_once(_config())
    second = project_access_sync.sync_project_access_once(_config())

    assert first["ack_errors"] == 1
    assert second["ack_errors"] == 0
    assert ack_attempts == 2
    with engine.connect() as conn:
        policy = project_access_service.get_project_policy(conn, project["id"])
    assert policy is not None
    assert policy["policy_revision"] == 1
    assert policy["last_applied_control_plane_revision"] == 1


def test_invalid_intent_is_rejected_with_exact_ack(monkeypatch, tmp_path) -> None:
    _engine, project = _create_project(tmp_path)
    ack_payloads: list[dict] = []

    def request(method, url, **kwargs):
        if url.endswith("/project-index"):
            return _Response({"projects": []})
        if url.endswith("/project-access-intents"):
            return _Response(
                {
                    "intents": [
                        {
                            "project_id": project["id"],
                            "revision": 5,
                            "mode": "unknown",
                            "bindings": [],
                        }
                    ]
                }
            )
        ack_payloads.append(kwargs["json"])
        return _Response({"project": {}})

    monkeypatch.setattr(project_access_sync.requests, "request", request)
    result = project_access_sync.sync_project_access_once(_config())

    assert result["rejected"] == 1
    assert ack_payloads == [
        {
            "project_id": project["id"],
            "revision": 5,
            "outcome": "rejected",
            "error_code": "invalid_project_access_mode",
        }
    ]


def test_malformed_wire_revisions_are_rejected_without_ack(monkeypatch, tmp_path) -> None:
    _engine, project = _create_project(tmp_path)
    ack_payloads: list[dict] = []

    malformed_intents = [
        {
            "project_id": project["id"],
            "mode": "restricted",
            "bindings": [],
        },
        {
            "project_id": project["id"],
            "revision": True,
            "mode": "restricted",
            "bindings": [],
        },
        {
            "project_id": project["id"],
            "revision": "0",
            "mode": "restricted",
            "bindings": [],
        },
    ]

    def request(method, url, **kwargs):
        if url.endswith("/project-index"):
            return _Response({"projects": []})
        if url.endswith("/project-access-intents"):
            return _Response({"intents": malformed_intents})
        ack_payloads.append(kwargs["json"])
        return _Response({"project": {}})

    monkeypatch.setattr(project_access_sync.requests, "request", request)
    result = project_access_sync.sync_project_access_once(_config())

    assert result["rejected"] == 3
    assert result["ack_errors"] == 0
    assert ack_payloads == []


def test_stale_intent_does_not_revert_or_ack(monkeypatch, tmp_path) -> None:
    engine, project = _create_project(tmp_path)
    with engine.begin() as conn:
        project_access_service.apply_project_access_intent(
            conn,
            {
                "project_id": project["id"],
                "revision": 3,
                "mode": "inherit",
                "bindings": [],
            },
        )
    acked = False

    def request(method, url, **kwargs):
        nonlocal acked
        if url.endswith("/project-index"):
            return _Response({"projects": []})
        if url.endswith("/project-access-intents"):
            return _Response(
                {
                    "intents": [
                        {
                            "project_id": project["id"],
                            "revision": 2,
                            "mode": "restricted",
                            "bindings": [],
                        }
                    ]
                }
            )
        acked = True
        return _Response({"project": {}})

    monkeypatch.setattr(project_access_sync.requests, "request", request)
    result = project_access_sync.sync_project_access_once(_config())

    assert result["stale"] == 1
    assert acked is False
    with engine.connect() as conn:
        policy = project_access_service.get_project_policy(conn, project["id"])
    assert policy is not None
    assert policy["mode"] == "inherit"
    assert policy["last_applied_control_plane_revision"] == 3


def test_unconfigured_sync_is_a_noop(monkeypatch) -> None:
    monkeypatch.setattr(
        project_access_sync.requests,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )

    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    result = project_access_sync.sync_project_access_once(config)

    assert result["ok"] is False
    assert result["configured"] is False
