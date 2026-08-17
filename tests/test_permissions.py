from __future__ import annotations

import json

import pytest
import requests

from config.v2_config import (
    AgentsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)
from tests.ui_server_test_helpers import csrf_headers
from vibe import permissions
from vibe.authorization import http_authorization_policy
from vibe.ui_server import app


def _config(instance_id: str = "inst-123") -> V2Config:
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
    cloud.backend_url = "https://backend.example"
    cloud.instance_id = instance_id
    cloud.instance_secret = "device-secret"
    return config


def _projection(instance_id: str = "inst-123") -> dict:
    return {
        "schema_version": 1,
        "instance": {
            "id": instance_id,
            "access_mode": "allowlist",
            "permission_authority": "instance",
            "local_mutation_allowed": True,
            "authorization_revision": 3,
        },
        "capabilities": [
            "instance.permissions.read",
            "instance.permissions.mutate",
        ],
        "access": {
            "owner": {"email": "owner@example.com", "role": "owner"},
            "entries": [],
        },
        "directory": {"members": [], "groups": []},
        "projects": [],
        "policy_sync": {
            "status": "none",
            "projects": {"active": 0, "error": 0, "offline": 0, "applying": 0, "in_sync": 0},
            "resources": {"active": 0, "error": 0, "offline": 0, "applying": 0, "in_sync": 0},
        },
    }


class _Response:
    def __init__(self, status: int, payload: object):
        self.status_code = status
        self._payload = payload
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


class _InvalidJsonResponse(_Response):
    def json(self):
        raise ValueError("not JSON")


def test_permissions_client_binds_requests_to_paired_instance(monkeypatch) -> None:
    captured = []

    def request(method, url, **kwargs):
        captured.append((method, url, kwargs))
        return _Response(200, _projection())

    monkeypatch.setattr(permissions.requests, "request", request)

    result = permissions.get_current_permissions(_config())

    assert result.source == "live"
    assert result.projection["instance"]["id"] == "inst-123"
    assert captured == [
        (
            "GET",
            "https://backend.example/api/v1/instances/inst-123/permissions",
            {
                "json": None,
                "headers": {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "avibe/dev",
                    "X-Vibe-Device-Secret": "device-secret",
                },
                "timeout": permissions.DEFAULT_TIMEOUT_SECONDS,
                "allow_redirects": False,
            },
        )
    ]


def test_permissions_offline_cache_is_sanitized_and_exact_instance_bound(monkeypatch) -> None:
    live = _projection()
    live["debug"] = {"instance_secret": "must-not-persist", "note": "kept"}
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, live),
    )
    permissions.get_current_permissions(_config())

    cache = json.loads(permissions._cache_path().read_text(encoding="utf-8"))  # noqa: SLF001
    assert "must-not-persist" not in json.dumps(cache)
    assert cache["projection"]["debug"] == {"note": "kept"}
    assert permissions._cache_path().stat().st_mode & 0o777 == 0o600  # noqa: SLF001

    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError()),
    )
    cached = permissions.get_current_permissions(_config())
    assert cached.source == "cache"
    assert cached.offline is True

    with pytest.raises(permissions.PermissionsUnavailableError):
        permissions.get_current_permissions(_config("inst-other"))


def test_permissions_offline_cache_covers_non_json_backend_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection()),
    )
    permissions.get_current_permissions(_config())
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _InvalidJsonResponse(503, None),
    )

    cached = permissions.get_current_permissions(_config())

    assert cached.source == "cache"
    assert cached.offline is True


def test_permissions_live_read_survives_cache_write_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection()),
    )
    monkeypatch.setattr(
        permissions,
        "_write_cache",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only state")),
    )

    result = permissions.get_current_permissions(_config())

    assert result.source == "live"
    assert result.projection["instance"]["id"] == "inst-123"


def test_permissions_does_not_mask_credential_failures_with_cache(monkeypatch) -> None:
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection()),
    )
    permissions.get_current_permissions(_config())
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(401, {"error": "invalid_device_secret"}),
    )

    with pytest.raises(permissions.PermissionsBackendError) as caught:
        permissions.get_current_permissions(_config())

    assert caught.value.status == 401
    assert caught.value.payload == {"error": "invalid_device_secret"}


def test_permissions_rejects_backend_projection_for_another_instance(monkeypatch) -> None:
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection("inst-other")),
    )

    with pytest.raises(permissions.PermissionsInvalidResponseError, match="permissions_instance_mismatch"):
        permissions.get_current_permissions(_config())


def test_authorized_users_mutation_refreshes_offline_cache(monkeypatch) -> None:
    live = _projection()
    live["access"]["entries"] = [
        {"kind": "email", "value": "old@example.com", "role": "viewer"}
    ]
    updated_entries = [
        {"kind": "email", "value": "new@example.com", "role": "editor"}
    ]
    backend_available = True

    def request(method, _url, **_kwargs):
        if method == "GET":
            if not backend_available:
                raise requests.ConnectionError()
            return _Response(200, live)
        return _Response(
            200,
            {
                "ok": True,
                "entries": updated_entries,
                "authorization_revision": 4,
            },
        )

    monkeypatch.setattr(permissions.requests, "request", request)
    config = _config()
    permissions.get_current_permissions(config)

    permissions.replace_authorized_users(
        {"entries": updated_entries, "if_match_revision": 3},
        config,
    )
    permissions._cache_projection("inst-123", live)  # noqa: SLF001
    backend_available = False
    cached = permissions.get_current_permissions(config)

    assert cached.source == "cache"
    assert cached.projection["instance"]["authorization_revision"] == 4
    assert cached.projection["access"]["entries"] == updated_entries


def test_project_access_mutation_refreshes_offline_cache(monkeypatch) -> None:
    live = _projection()
    project = {
        "project_id": "project-1",
        "organization_id": "org-1",
        "display_name": "Launch Plan",
        "access": {"mode": "restricted", "revision": 1, "bindings": []},
        "sync": {
            "status": "in_sync",
            "desired_access_revision": 1,
            "applied_access_revision": 1,
            "last_synced_at": None,
        },
    }
    live["projects"] = [project]
    updated_project = {
        **project,
        "access": {"mode": "owner_only", "revision": 2, "bindings": []},
    }
    backend_available = True

    def request(method, _url, **_kwargs):
        if method == "GET":
            if not backend_available:
                raise requests.ConnectionError()
            return _Response(200, live)
        return _Response(
            200,
            {
                "ok": True,
                "project": updated_project,
                "authorization_revision": 4,
            },
        )

    monkeypatch.setattr(permissions.requests, "request", request)
    config = _config()
    permissions.get_current_permissions(config)

    permissions.update_project_access(
        "project-1",
        {"mode": "owner_only", "bindings": [], "if_match_revision": 1},
        config,
    )
    backend_available = False
    cached = permissions.get_current_permissions(config)

    assert cached.source == "cache"
    assert cached.projection["instance"]["authorization_revision"] == 4
    assert cached.projection["projects"] == [updated_project]


def test_permissions_http_policy_allows_viewer_reads_but_owner_only_mutations() -> None:
    assert http_authorization_policy("GET", "/api/permissions").minimum_role == "viewer"
    assert (
        http_authorization_policy("PUT", "/api/permissions/authorized-users").minimum_role
        == "owner"
    )
    assert (
        http_authorization_policy("PUT", "/api/permissions/projects/project-1/access").minimum_role
        == "owner"
    )


def test_permissions_same_origin_routes_reject_non_contract_entry_fields(monkeypatch) -> None:
    client = app.test_client()
    headers = csrf_headers(client)
    called = False

    def replace(_payload):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(permissions, "replace_authorized_users", replace)
    response = client.put(
        "/api/permissions/authorized-users",
        json={
            "entries": [
                {
                    "kind": "email",
                    "value": "viewer@example.com",
                    "role": "viewer",
                    "instance_id": "inst-other",
                }
            ],
            "if_match_revision": 3,
        },
        headers=headers,
    )

    assert response.status_code == 422
    assert response.get_json() == {"ok": False, "error": "invalid_request"}
    assert called is False


def test_permissions_same_origin_routes_forward_revision_and_conflict(monkeypatch) -> None:
    client = app.test_client()
    headers = csrf_headers(client)
    captured = {}

    def update(project_id, payload):
        captured.update(project_id=project_id, payload=payload)
        raise permissions.PermissionsBackendError(
            409,
            {"error": "permission_revision_conflict", "current_revision": 7},
        )

    monkeypatch.setattr(permissions, "update_project_access", update)
    response = client.put(
        "/api/permissions/projects/project-1/access",
        json={"mode": "inherit", "bindings": [], "if_match_revision": 6},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "ok": False,
        "error": "permission_revision_conflict",
        "current_revision": 7,
    }
    assert captured == {
        "project_id": "project-1",
        "payload": {"mode": "inherit", "bindings": [], "if_match_revision": 6},
    }
