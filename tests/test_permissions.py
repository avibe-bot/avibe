from __future__ import annotations

from copy import deepcopy
import json
import multiprocessing as mp
import os
from typing import Any

import pytest
import requests

from config import atomic_io
from config.v2_config import (
    AgentsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)
from storage.lock import MigrationLockTimeout
from tests.ui_server_test_helpers import (
    csrf_headers,
    remote_peer,
    remote_session_cookie,
    save_config,
)
from vibe import permissions, remote_access
from vibe.authorization import http_authorization_policy
from vibe.sse_broker import broker
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
    config.save()
    return config


def _cache_projection_process(
    projection: dict[str, Any],
    entered_write,
    release_write,
) -> None:
    original_write = permissions._write_cache  # noqa: SLF001

    if entered_write is not None:
        def delayed_write(
            instance_id: str,
            candidate: dict[str, Any],
            *,
            cache_order: int = 0,
            mutation_orders: dict[str, Any] | None = None,
        ) -> None:
            entered_write.set()
            if not release_write.wait(10):
                raise TimeoutError("cache write was not released")
            original_write(
                instance_id,
                candidate,
                cache_order=cache_order,
                mutation_orders=mutation_orders,
            )

        permissions._write_cache = delayed_write  # noqa: SLF001

    permissions._cache_projection("inst-123", projection)  # noqa: SLF001


def _delayed_cache_projection_process(
    projection: dict[str, Any],
    request_order: int,
    ready,
    release,
) -> None:
    ready.set()
    if not release.wait(10):
        raise TimeoutError("cache response was not released")
    permissions._cache_projection(  # noqa: SLF001
        "inst-123",
        projection,
        request_order=request_order,
    )


def _cache_mutation_rebase_process(
    access_entries: list[dict[str, str]],
    entered_lock,
    release_lock,
) -> None:
    original_lock = permissions._cache_file_lock  # noqa: SLF001

    def delayed_lock(path):
        entered_lock.set()
        if not release_lock.wait(10):
            raise TimeoutError("cache lock was not released")
        return original_lock(path)

    permissions._cache_file_lock = delayed_lock  # noqa: SLF001
    permissions._cache_mutation_result(  # noqa: SLF001
        "inst-123",
        3,
        access_entries=access_entries,
    )


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


def _complete_projection(instance_id: str = "inst-123") -> dict:
    projection = _projection(instance_id)
    projection["instance"].update(
        {
            "name": "max-incus-1",
            "public_url": "https://max-incus-1-app.avibe.bot",
            "organization": {"id": "org-1", "name": "CoinSummer"},
        }
    )
    projection["access"]["entries"] = [
        {"kind": "email", "value": "viewer@example.com", "role": "viewer"}
    ]
    projection["directory"] = {
        "members": [
            {
                "id": "member-1",
                "email": "member@example.com",
                "organization_role": "member",
                "group_ids": ["group-1"],
            }
        ],
        "groups": [
            {
                "id": "group-1",
                "name": "Launch Team",
                "archived_at": None,
            }
        ],
    }
    projection["projects"] = [
        {
            "project_id": "project-1",
            "organization_id": "org-1",
            "display_name": "Launch Plan",
            "access": {
                "mode": "restricted",
                "revision": 2,
                "bindings": [
                    {
                        "principal_kind": "organization_group",
                        "principal_value": "group-1",
                        "access_role": "viewer",
                    }
                ],
            },
            "sync": {
                "status": "in_sync",
                "desired_access_revision": 2,
                "applied_access_revision": 2,
                "last_synced_at": "2026-08-17T12:00:00.000Z",
                "last_sync_error": "previous error",
            },
        }
    ]
    projection["policy_sync"] = {
        "status": "in_sync",
        "projects": {"active": 1, "error": 0, "offline": 0, "applying": 0, "in_sync": 1},
        "resources": {"active": 0, "error": 0, "offline": 0, "applying": 0, "in_sync": 0},
    }
    return projection


def _resource(
    instance_id: str = "inst-123",
    resource_kind: str = "agent",
    resource_id: str = "agent-1",
    *,
    revision: int = 4,
) -> dict:
    return {
        "instance_id": instance_id,
        "resource_kind": resource_kind,
        "resource_id": resource_id,
        "display_name": "Agent",
        "owner_user_id": "owner-1",
        "access": {
            "access_level": "scope",
            "group_ids": ["group-1"],
            "revision": revision,
        },
        "sync": {
            "status": "in_sync",
            "desired_acl_revision": revision,
            "applied_acl_revision": revision,
            "last_synced_at": "2026-08-18T06:00:00.000Z",
        },
    }


_MISSING = object()


NONEMPTY_PROJECTION_STRING_PATHS = [
    pytest.param(("access", "owner", "email"), id="owner-email"),
    pytest.param(("access", "entries", 0, "value"), id="access-principal"),
    pytest.param(("directory", "members", 0, "id"), id="member-id"),
    pytest.param(("directory", "members", 0, "email"), id="member-email"),
    pytest.param(("directory", "members", 0, "group_ids", 0), id="member-group-id"),
    pytest.param(("directory", "groups", 0, "id"), id="group-id"),
    pytest.param(("projects", 0, "project_id"), id="project-id"),
    pytest.param(("projects", 0, "organization_id"), id="project-organization-id"),
    pytest.param(
        ("projects", 0, "access", "bindings", 0, "principal_value"),
        id="project-principal",
    ),
]

INVALID_PROJECT_ROUTE_IDS = [
    pytest.param(".", id="current-directory-segment"),
    pytest.param("..", id="parent-directory-segment"),
    pytest.param("project/child", id="forward-embedded"),
    pytest.param("/project", id="forward-leading"),
    pytest.param("project/", id="forward-trailing"),
    pytest.param(r"project\child", id="backslash-embedded"),
    pytest.param(r"\project", id="backslash-leading"),
    pytest.param("project\\", id="backslash-trailing"),
]

PROJECTION_IDENTITY_COLLECTION_PATHS = [
    pytest.param(("access", "entries"), id="access-principals"),
    pytest.param(("directory", "members"), id="directory-members"),
    pytest.param(("directory", "members", 0, "group_ids"), id="member-group-ids"),
    pytest.param(("directory", "groups"), id="directory-groups"),
    pytest.param(("projects",), id="projects"),
    pytest.param(("projects", 0, "access", "bindings"), id="project-bindings"),
]


def _replace_nested(value: dict, path: tuple[str | int, ...], replacement: object) -> None:
    parent: Any = value
    for key in path[:-1]:
        parent = parent[key]
    key = path[-1]
    if replacement is _MISSING:
        del parent[key]
    else:
        parent[key] = replacement


MALFORMED_PROJECTION_CASES = [
    pytest.param(("schema_version",), True, id="schema-version"),
    pytest.param(("instance",), None, id="instance-container"),
    pytest.param(("instance", "id"), 123, id="instance-id"),
    pytest.param(("instance", "access_mode"), "private", id="instance-access-mode"),
    pytest.param(("instance", "permission_authority"), "local", id="permission-authority"),
    pytest.param(("instance", "local_mutation_allowed"), 1, id="local-mutation-flag"),
    pytest.param(("instance", "authorization_revision"), True, id="authorization-revision"),
    pytest.param(("instance", "name"), "", id="instance-name"),
    pytest.param(
        ("instance", "public_url"),
        "http://max-incus-1-app.avibe.bot",
        id="instance-public-url",
    ),
    pytest.param(("instance", "organization"), {}, id="instance-organization"),
    pytest.param(
        ("instance", "organization", "id"),
        "",
        id="instance-organization-id",
    ),
    pytest.param(
        ("instance", "organization", "name"),
        123,
        id="instance-organization-name",
    ),
    pytest.param(("capabilities",), None, id="capabilities-container"),
    pytest.param(("capabilities", 0), 123, id="capability-type"),
    pytest.param(("capabilities", 0), "", id="capability-empty"),
    pytest.param(
        ("capabilities",),
        ["instance.permissions.mutate"],
        id="required-read-capability",
    ),
    pytest.param(("access",), None, id="access-container"),
    pytest.param(("access", "owner"), None, id="owner-container"),
    pytest.param(("access", "owner", "email"), 123, id="owner-email"),
    pytest.param(("access", "owner", "role"), "viewer", id="owner-role"),
    pytest.param(("access", "entries"), None, id="access-entries-container"),
    pytest.param(("access", "entries", 0), "entry", id="access-entry-container"),
    pytest.param(("access", "entries", 0, "kind"), "user", id="access-entry-kind"),
    pytest.param(("access", "entries", 0, "value"), 123, id="access-entry-value"),
    pytest.param(("access", "entries", 0, "role"), "owner", id="access-entry-role"),
    pytest.param(("directory",), None, id="directory-container"),
    pytest.param(("directory", "members"), None, id="members-container"),
    pytest.param(("directory", "members", 0), "member", id="member-container"),
    pytest.param(("directory", "members", 0, "id"), 123, id="member-id"),
    pytest.param(("directory", "members", 0, "email"), 123, id="member-email"),
    pytest.param(
        ("directory", "members", 0, "organization_role"),
        "viewer",
        id="member-organization-role",
    ),
    pytest.param(("directory", "members", 0, "group_ids"), None, id="member-groups"),
    pytest.param(("directory", "members", 0, "group_ids", 0), 123, id="member-group-id"),
    pytest.param(("directory", "groups"), None, id="groups-container"),
    pytest.param(("directory", "groups", 0), "group", id="group-container"),
    pytest.param(("directory", "groups", 0, "id"), 123, id="group-id"),
    pytest.param(("directory", "groups", 0, "name"), 123, id="group-name"),
    pytest.param(("directory", "groups", 0, "archived_at"), 123, id="group-archived-at"),
    pytest.param(("projects",), None, id="projects-container"),
    pytest.param(("projects", 0), "project", id="project-container"),
    pytest.param(("projects", 0, "project_id"), 123, id="project-id"),
    pytest.param(("projects", 0, "organization_id"), 123, id="project-organization-id"),
    pytest.param(("projects", 0, "display_name"), 123, id="project-display-name"),
    pytest.param(("projects", 0, "access"), None, id="project-access-container"),
    pytest.param(("projects", 0, "access", "mode"), "public", id="project-access-mode"),
    pytest.param(
        ("projects", 0, "access", "mode"),
        "owner_only",
        id="project-access-ui-mode",
    ),
    pytest.param(("projects", 0, "access", "revision"), True, id="project-access-revision"),
    pytest.param(("projects", 0, "access", "bindings"), None, id="bindings-container"),
    pytest.param(("projects", 0, "access", "bindings", 0), "binding", id="binding-container"),
    pytest.param(
        ("projects", 0, "access", "bindings", 0, "principal_kind"),
        "user",
        id="binding-kind",
    ),
    pytest.param(
        ("projects", 0, "access", "bindings", 0, "principal_value"),
        123,
        id="binding-value",
    ),
    pytest.param(
        ("projects", 0, "access", "bindings", 0, "access_role"),
        "owner",
        id="binding-role",
    ),
    pytest.param(("projects", 0, "sync"), None, id="project-sync-container"),
    pytest.param(("projects", 0, "sync", "status"), "unknown", id="project-sync-status"),
    pytest.param(
        ("projects", 0, "sync", "status"),
        "applying",
        id="aggregate-only-applying-status",
    ),
    pytest.param(
        ("projects", 0, "sync", "desired_access_revision"),
        True,
        id="desired-access-revision",
    ),
    pytest.param(
        ("projects", 0, "sync", "applied_access_revision"),
        -1,
        id="applied-access-revision",
    ),
    pytest.param(("projects", 0, "sync", "last_synced_at"), 123, id="last-synced-at"),
    pytest.param(("projects", 0, "sync", "last_sync_error"), None, id="last-sync-error"),
    pytest.param(("policy_sync",), None, id="policy-sync-container"),
    pytest.param(("policy_sync", "status"), "pending", id="policy-sync-status"),
    pytest.param(("policy_sync", "projects"), None, id="project-counts-container"),
    pytest.param(("policy_sync", "resources"), None, id="resource-counts-container"),
    pytest.param(("policy_sync", "projects", "active"), True, id="sync-count-value"),
    pytest.param(("policy_sync", "resources", "in_sync"), _MISSING, id="sync-count-required-field"),
]


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


def test_resource_access_client_binds_identity_and_strips_local_pairing_precondition(
    monkeypatch,
) -> None:
    captured = []

    def request(method, url, **kwargs):
        captured.append((method, url, kwargs.get("json")))
        resource = _resource(revision=5 if method == "PUT" else 4)
        return _Response(
            200,
            {"ok": True, "resource": resource} if method == "PUT" else {"resource": resource},
        )

    monkeypatch.setattr(permissions.requests, "request", request)

    read = permissions.get_resource_access("agent", "agent-1", _config())
    written = permissions.update_resource_access(
        "agent",
        "agent-1",
        {
            "access_level": "scope",
            "group_ids": ["group-1"],
            "if_match_revision": 4,
            "if_match_instance_id": "inst-123",
        },
        _config(),
    )

    expected_url = (
        "https://backend.example/api/v1/instances/inst-123/permissions/"
        "resources/agent/agent-1/access"
    )
    assert read == {"resource": _resource(revision=4)}
    assert written == {"ok": True, "resource": _resource(revision=5)}
    assert captured == [
        ("GET", expected_url, None),
        (
            "PUT",
            expected_url,
            {
                "access_level": "scope",
                "group_ids": ["group-1"],
                "if_match_revision": 4,
            },
        ),
    ]


@pytest.mark.parametrize("operation", ["get", "put"])
def test_resource_access_client_rejects_retired_kind_before_backend(
    monkeypatch,
    operation,
) -> None:
    """§3.2 retired show_page from the Resource ACL: the retired kind fails
    client-side as an invalid resource before any Backend request is made."""
    called = False

    def request(*_args, **_kwargs):
        nonlocal called
        called = True
        return _Response(200, {"resource": _resource()})

    monkeypatch.setattr(permissions.requests, "request", request)

    with pytest.raises(
        permissions.PermissionsInvalidRequestError,
        match="invalid_resource_kind",
    ):
        if operation == "get":
            permissions.get_resource_access("show_page", "page-1", _config())
        else:
            permissions.update_resource_access(
                "show_page",
                "page-1",
                {
                    "access_level": "scope",
                    "group_ids": ["group-1"],
                    "if_match_revision": 4,
                    "if_match_instance_id": "inst-123",
                },
                _config(),
            )

    assert called is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instance_id", "inst-other"),
        ("resource_kind", "vault_secret"),
        ("resource_id", "agent-other"),
    ],
)
def test_resource_access_client_rejects_response_identity_mismatch(
    monkeypatch,
    field,
    value,
) -> None:
    resource = _resource()
    resource[field] = value
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, {"resource": resource}),
    )

    with pytest.raises(
        permissions.PermissionsInvalidResponseError,
        match="permissions_resource_mismatch",
    ):
        permissions.get_resource_access("agent", "agent-1", _config())


@pytest.mark.parametrize("operation", ["get", "put"])
def test_resource_access_rechecks_pairing_after_response_validation(
    monkeypatch,
    operation: str,
) -> None:
    config = _config()
    original_validate = permissions._validate_resource  # noqa: SLF001

    def validate_and_repair(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        replacement = V2Config.load()
        replacement.remote_access.vibe_cloud.instance_id = "inst-new"
        replacement.save()
        return result

    monkeypatch.setattr(permissions, "_validate_resource", validate_and_repair)
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda method, *_args, **_kwargs: _Response(
            200,
            {
                **({"ok": True} if method == "PUT" else {}),
                "resource": _resource(revision=5 if method == "PUT" else 4),
            },
        ),
    )

    with pytest.raises(
        permissions.PermissionsPairingChangedError,
        match="permissions_pairing_changed",
    ):
        if operation == "get":
            permissions.get_resource_access("agent", "agent-1", config)
        else:
            permissions.update_resource_access(
                "agent",
                "agent-1",
                {
                    "access_level": "scope",
                    "group_ids": ["group-1"],
                    "if_match_revision": 4,
                    "if_match_instance_id": "inst-123",
                },
                config,
            )


def test_permissions_response_enriches_legacy_display_from_the_exact_pairing() -> None:
    config = _config()
    config.remote_access.vibe_cloud.public_url = "https://max-incus-1-app.avibe.bot"
    projection = _projection()

    payload = permissions.response_payload(
        permissions.PermissionsProjectionResult(projection=projection, source="live"),
        config,
    )

    assert payload["projection"]["instance"] == {
        **projection["instance"],
        "name": "max-incus-1",
        "public_url": "https://max-incus-1-app.avibe.bot",
    }
    assert "organization" not in payload["projection"]["instance"]
    assert "name" not in projection["instance"]


def test_permissions_response_does_not_mix_display_metadata_across_pairings() -> None:
    config = _config("inst-other")
    config.remote_access.vibe_cloud.public_url = "https://other-app.avibe.bot"
    projection = _projection()

    payload = permissions.response_payload(
        permissions.PermissionsProjectionResult(projection=projection, source="cache"),
        config,
    )

    assert payload["projection"] is projection
    assert "name" not in payload["projection"]["instance"]


def test_permissions_preserves_authoritative_instance_display_metadata() -> None:
    projection = _complete_projection()
    config = _config()
    config.remote_access.vibe_cloud.public_url = "https://stale-local-app.avibe.bot"

    payload = permissions.response_payload(
        permissions.PermissionsProjectionResult(projection=projection, source="live"),
        config,
    )

    assert payload["projection"]["instance"] == projection["instance"]


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
    if os.name == "posix":
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


def test_permissions_cache_remains_atomic_without_os_fchmod(monkeypatch) -> None:
    monkeypatch.delattr(atomic_io.os, "fchmod", raising=False)
    projection = _complete_projection()
    config = _config()
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, projection),
    )

    result = permissions.get_current_permissions(config)
    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert result.source == "live"
    assert cached is not None
    assert cached.projection == projection

    newer = _complete_projection()
    newer["instance"]["authorization_revision"] = 4
    monkeypatch.setattr(
        atomic_io.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, newer),
    )

    live = permissions.get_current_permissions(config)
    retained = permissions._read_cache("inst-123")  # noqa: SLF001

    assert live.projection == newer
    assert retained is not None
    assert retained.projection == projection
    cache_path = permissions._cache_path()  # noqa: SLF001
    assert list(cache_path.parent.glob(f".{cache_path.name}.*")) == []


def test_permissions_cache_revision_is_monotonic_across_processes() -> None:
    stale = _complete_projection()
    stale["instance"]["authorization_revision"] = 3
    newer = _complete_projection()
    newer["instance"]["authorization_revision"] = 4
    context = mp.get_context("spawn")
    entered_write = context.Event()
    release_write = context.Event()
    stale_writer = context.Process(
        target=_cache_projection_process,
        args=(stale, entered_write, release_write),
    )
    newer_writer = context.Process(
        target=_cache_projection_process,
        args=(newer, None, None),
    )

    try:
        stale_writer.start()
        assert entered_write.wait(10)
        newer_writer.start()
        release_write.set()
        stale_writer.join(10)
        newer_writer.join(10)
        assert stale_writer.exitcode == 0
        assert newer_writer.exitcode == 0
    finally:
        release_write.set()
        for process in (stale_writer, newer_writer):
            if process.is_alive():
                process.terminate()
            process.join(5)

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.projection["instance"]["authorization_revision"] == 4


def test_permissions_cache_equal_revision_retains_the_newest_complete_projection() -> None:
    stale = _complete_projection()
    stale["directory"]["members"][0]["email"] = "stale-member@example.com"
    stale["projects"][0]["display_name"] = "Stale Project"
    stale["projects"][0]["access"]["revision"] = 3
    stale["projects"][0]["sync"].update(
        {
            "status": "pending",
            "desired_access_revision": 3,
            "applied_access_revision": 2,
            "last_synced_at": None,
        }
    )
    stale["policy_sync"] = {
        "status": "applying",
        "projects": {"active": 1, "error": 0, "offline": 0, "applying": 1, "in_sync": 0},
        "resources": {"active": 1, "error": 0, "offline": 0, "applying": 1, "in_sync": 0},
    }
    stale_cycle = deepcopy(stale["projects"][0])
    stale_cycle["project_id"] = "project-cycle"
    stale_cycle["display_name"] = "Stale Cycle"
    stale_cycle["access"]["revision"] = 2
    stale_cycle["sync"].update(
        {
            "status": "in_sync",
            "desired_access_revision": 2,
            "applied_access_revision": 2,
            "last_synced_at": "2026-08-18T09:00:00.000Z",
        }
    )
    stale["projects"].append(stale_cycle)
    fresh = deepcopy(stale)
    fresh["directory"]["members"][0]["email"] = "fresh-member@example.com"
    fresh["projects"][0]["display_name"] = "Fresh Project"
    fresh["projects"][0]["sync"].update(
        {
            "status": "in_sync",
            "applied_access_revision": 3,
            "last_synced_at": "2026-08-18T10:00:00.000Z",
        }
    )
    fresh_cycle = fresh["projects"][1]
    fresh_cycle["display_name"] = "Fresh Cycle"
    fresh_cycle["access"]["revision"] = 3
    fresh_cycle["sync"].update(
        {
            "status": "pending",
            "desired_access_revision": 3,
            "applied_access_revision": 2,
        }
    )
    fresh["policy_sync"] = {
        "status": "in_sync",
        "projects": {"active": 1, "error": 0, "offline": 0, "applying": 0, "in_sync": 1},
        "resources": {"active": 1, "error": 0, "offline": 0, "applying": 0, "in_sync": 1},
    }
    stale_order = permissions._cache_allocate_order("inst-123")  # noqa: SLF001
    fresh_order = permissions._cache_allocate_order("inst-123")  # noqa: SLF001
    context = mp.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    stale_writer = context.Process(
        target=_delayed_cache_projection_process,
        args=(stale, stale_order, ready, release),
    )

    try:
        stale_writer.start()
        assert ready.wait(10)
        permissions._cache_projection(  # noqa: SLF001
            "inst-123",
            fresh,
            request_order=fresh_order,
        )
        release.set()
        stale_writer.join(10)
        assert stale_writer.exitcode == 0
    finally:
        release.set()
        if stale_writer.is_alive():
            stale_writer.terminate()
        stale_writer.join(5)

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.cache_order == fresh_order
    assert cached.projection["directory"] == fresh["directory"]
    assert cached.projection["projects"] == fresh["projects"]
    assert cached.projection["policy_sync"] == fresh["policy_sync"]


@pytest.mark.parametrize("terminal_status", ["error", "offline"])
def test_permissions_cache_equal_revision_preserves_terminal_sync_failures(
    terminal_status: str,
) -> None:
    applying = _complete_projection()
    applying["policy_sync"]["status"] = "applying"
    applying["projects"][0]["sync"].update(
        {
            "status": "pending",
            "desired_access_revision": 3,
            "applied_access_revision": 2,
        }
    )
    terminal = deepcopy(applying)
    terminal["policy_sync"]["status"] = terminal_status
    terminal["projects"][0]["sync"].update(
        {
            "status": terminal_status,
            "last_sync_error": f"{terminal_status} while applying",
        }
    )

    permissions._cache_projection(  # noqa: SLF001
        "inst-123",
        applying,
        request_order=10,
    )
    permissions._cache_projection(  # noqa: SLF001
        "inst-123",
        terminal,
        request_order=11,
    )
    # A delayed response from the older request must not roll the terminal
    # state back to the in-progress snapshot.
    permissions._cache_projection(  # noqa: SLF001
        "inst-123",
        applying,
        request_order=10,
    )

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.projection["policy_sync"]["status"] == terminal_status
    assert cached.projection["projects"][0]["sync"]["status"] == terminal_status
    assert cached.projection["projects"][0]["sync"]["last_sync_error"] == (
        f"{terminal_status} while applying"
    )


def test_permissions_cache_mutation_rebase_reads_complete_equal_revision_projection() -> None:
    base = _complete_projection()
    complete = deepcopy(base)
    complete["directory"]["members"][0]["email"] = "new-member@example.com"
    complete["projects"][0]["display_name"] = "Newest Project"
    complete["policy_sync"]["projects"]["active"] = 7
    updated_entries = [
        {"kind": "email", "value": "updated@example.com", "role": "editor"}
    ]
    permissions._cache_projection("inst-123", base)  # noqa: SLF001

    context = mp.get_context("spawn")
    entered_lock = context.Event()
    release_lock = context.Event()
    mutation = context.Process(
        target=_cache_mutation_rebase_process,
        args=(updated_entries, entered_lock, release_lock),
    )

    try:
        mutation.start()
        assert entered_lock.wait(10)
        permissions._cache_projection("inst-123", complete)  # noqa: SLF001
        release_lock.set()
        mutation.join(10)
        assert mutation.exitcode == 0
    finally:
        release_lock.set()
        if mutation.is_alive():
            mutation.terminate()
        mutation.join(5)

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.projection["directory"] == complete["directory"]
    assert cached.projection["projects"] == complete["projects"]
    assert cached.projection["policy_sync"] == complete["policy_sync"]
    assert cached.projection["access"]["entries"] == updated_entries


def test_permissions_cache_mutation_rebase_preserves_newer_project_sync_state() -> None:
    newer = _complete_projection()
    newer_project = newer["projects"][0]
    newer_project["access"] = {
        **newer_project["access"],
        "mode": "restricted",
        "revision": 4,
        "bindings": [
            {
                "principal_kind": "email",
                "principal_value": "new@example.com",
                "access_role": "editor",
            }
        ],
    }
    newer_project["sync"] = {
        **newer_project["sync"],
        "status": "in_sync",
        "desired_access_revision": 4,
        "applied_access_revision": 4,
    }
    mutation_project = deepcopy(newer_project)
    mutation_project["sync"] = {
        **mutation_project["sync"],
        "status": "pending",
        "applied_access_revision": 3,
    }

    permissions._cache_projection(  # noqa: SLF001
        "inst-123",
        newer,
        request_order=20,
    )
    permissions._cache_mutation_result(  # noqa: SLF001
        "inst-123",
        newer["instance"]["authorization_revision"],
        project=mutation_project,
        request_order=19,
    )

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.projection["projects"][0]["access"] == mutation_project["access"]
    assert cached.projection["projects"][0]["sync"] == newer_project["sync"]


def test_permissions_cache_mutation_rebase_preserves_newer_same_project_policy() -> None:
    base = _complete_projection()
    permissions._cache_projection(  # noqa: SLF001
        "inst-123",
        base,
        request_order=18,
    )
    delayed = deepcopy(base["projects"][0])
    delayed["display_name"] = "Delayed Project"
    delayed["access"] = {
        **delayed["access"],
        "mode": "inherit",
        "revision": 3,
        "bindings": [],
    }
    delayed["sync"] = {
        **delayed["sync"],
        "status": "pending",
        "desired_access_revision": 3,
        "applied_access_revision": 2,
    }
    newer = deepcopy(delayed)
    newer["display_name"] = "Newest Project"
    newer["access"] = {
        **newer["access"],
        "mode": "restricted",
        "revision": 4,
        "bindings": [
            {
                "principal_kind": "email",
                "principal_value": "newest@example.com",
                "access_role": "editor",
            }
        ],
    }
    newer["sync"] = {
        **newer["sync"],
        "status": "error",
        "desired_access_revision": 4,
        "applied_access_revision": 3,
        "last_sync_error": "latest failure",
    }

    # The R+1 acknowledgement wins the cache before the delayed R response arrives.
    permissions._cache_mutation_result(  # noqa: SLF001
        "inst-123",
        5,
        project=newer,
        request_order=20,
    )
    permissions._cache_mutation_result(  # noqa: SLF001
        "inst-123",
        4,
        project=delayed,
        request_order=19,
    )

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.cache_order == 20
    assert cached.projection["instance"]["authorization_revision"] == 5
    assert cached.projection["projects"][0] == newer


def test_permissions_cache_mutation_rebase_preserves_newer_authorized_users_entries() -> None:
    base = _complete_projection()
    newer_entries = [
        {"kind": "email", "value": "newer@example.com", "role": "editor"}
    ]
    delayed_entries = [
        {"kind": "email", "value": "delayed@example.com", "role": "viewer"}
    ]
    permissions._cache_projection(  # noqa: SLF001
        "inst-123",
        base,
        request_order=18,
    )

    # The newer authorized-users mutation commits first; its delayed response
    # must remain authoritative for access entries across a process rebase.
    permissions._cache_mutation_result(  # noqa: SLF001
        "inst-123",
        5,
        access_entries=newer_entries,
        request_order=20,
    )
    permissions._cache_mutation_result(  # noqa: SLF001
        "inst-123",
        4,
        access_entries=delayed_entries,
        request_order=19,
    )

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.cache_order == 20
    assert cached.projection["instance"]["authorization_revision"] == 5
    assert cached.projection["access"]["entries"] == newer_entries


def test_permissions_live_read_rechecks_pairing_after_cache_write_failure(monkeypatch) -> None:
    config = _config()
    switched = False

    def failed_write(*_args, **_kwargs):
        nonlocal switched
        if not switched:
            switched = True
            replacement = V2Config.load()
            replacement.remote_access.vibe_cloud.instance_id = "inst-new"
            replacement.save()
        raise OSError("read-only state")

    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection()),
    )
    monkeypatch.setattr(permissions, "_write_cache", failed_write)

    with pytest.raises(
        permissions.PermissionsPairingChangedError,
        match="permissions_pairing_changed",
    ):
        permissions.get_current_permissions(config)


@pytest.mark.parametrize("entity", ["access", "project"])
def test_permissions_cache_replays_superseded_mutation_entity_without_lowering_revision(
    entity: str,
) -> None:
    newer = _complete_projection()
    newer["directory"]["members"][0]["email"] = "newer-directory@example.com"
    newer["policy_sync"]["status"] = "in_sync"
    newer["instance"]["authorization_revision"] = 4
    permissions._cache_projection("inst-123", newer, request_order=20)  # noqa: SLF001

    if entity == "access":
        entries = [{"kind": "email", "value": "delayed@example.com", "role": "editor"}]
        permissions._cache_mutation_result(  # noqa: SLF001
            "inst-123",
            3,
            access_entries=entries,
            request_order=19,
        )
    else:
        acknowledged = deepcopy(newer["projects"][0])
        acknowledged["display_name"] = "Delayed Project"
        acknowledged["access"]["revision"] = 5
        permissions._cache_mutation_result(  # noqa: SLF001
            "inst-123",
            3,
            project=acknowledged,
            request_order=19,
        )

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.projection["instance"]["authorization_revision"] == 4
    assert cached.projection["directory"] == newer["directory"]
    assert cached.projection["policy_sync"] == newer["policy_sync"]
    if entity == "access":
        assert cached.projection["access"]["entries"] == entries
    else:
        assert cached.projection["projects"][0]["display_name"] == "Delayed Project"


@pytest.mark.parametrize(
    "status",
    [*sorted(permissions._CACHE_FALLBACK_HTTP_STATUSES), 503],  # noqa: SLF001
)
@pytest.mark.parametrize("body_shape", ["invalid-json", "json-list"])
def test_permissions_offline_cache_covers_every_retryable_response_body_shape(
    monkeypatch,
    status: int,
    body_shape: str,
) -> None:
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection()),
    )
    permissions.get_current_permissions(_config())
    failure = (
        _InvalidJsonResponse(status, None)
        if body_shape == "invalid-json"
        else _Response(status, [])
    )
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: failure,
    )

    cached = permissions.get_current_permissions(_config())

    assert cached.source == "cache"
    assert cached.offline is True


def test_permissions_does_not_mask_non_json_authoritative_failure_with_cache(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection()),
    )
    permissions.get_current_permissions(_config())
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _InvalidJsonResponse(403, None),
    )

    with pytest.raises(permissions.PermissionsInvalidResponseError):
        permissions.get_current_permissions(_config())


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


def test_permissions_live_read_replaces_an_invalid_utf8_cache(monkeypatch) -> None:
    cache_path = permissions._cache_path()  # noqa: SLF001
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"\xff")
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection()),
    )

    result = permissions.get_current_permissions(_config())
    cached = permissions._read_cache("inst-123")  # noqa: SLF001

    assert result.source == "live"
    assert cached is not None
    assert cached.projection == result.projection


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, "invalid_device_secret"),
        (403, "instance_access_forbidden"),
        (404, "instance_not_found"),
    ],
)
def test_permissions_does_not_mask_non_retryable_failures_with_cache(
    monkeypatch,
    status: int,
    error: str,
) -> None:
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection()),
    )
    permissions.get_current_permissions(_config())
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(status, {"error": error}),
    )

    with pytest.raises(permissions.PermissionsBackendError) as caught:
        permissions.get_current_permissions(_config())

    assert caught.value.status == status
    assert caught.value.payload == {"error": error}


@pytest.mark.parametrize("status", [408, 425, 429])
def test_permissions_uses_exact_instance_cache_for_retryable_backend_reads(
    monkeypatch,
    status: int,
) -> None:
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection()),
    )
    permissions.get_current_permissions(_config())
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(status, {"error": "temporarily_unavailable"}),
    )

    cached = permissions.get_current_permissions(_config())

    assert cached.source == "cache"
    assert cached.offline is True
    assert cached.projection["instance"]["id"] == "inst-123"


def test_permissions_rejects_backend_projection_for_another_instance(monkeypatch) -> None:
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _projection("inst-other")),
    )

    with pytest.raises(permissions.PermissionsInvalidResponseError, match="permissions_instance_mismatch"):
        permissions.get_current_permissions(_config())


def test_permissions_mutations_reject_a_changed_pairing_before_backend_contact(
    monkeypatch,
) -> None:
    backend_calls = []
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *args, **kwargs: backend_calls.append((args, kwargs)),
    )
    changed_config = _config("inst-new")

    with pytest.raises(
        permissions.PermissionsPairingChangedError,
        match="permissions_pairing_changed",
    ):
        permissions.replace_authorized_users(
            {
                "entries": [],
                "if_match_revision": 0,
                "if_match_instance_id": "inst-old",
            },
            changed_config,
        )
    with pytest.raises(
        permissions.PermissionsPairingChangedError,
        match="permissions_pairing_changed",
    ):
        permissions.update_project_access(
            "project-1",
            {
                "mode": "inherit",
                "bindings": [],
                "if_match_revision": 0,
                "if_match_instance_id": "inst-old",
            },
            changed_config,
        )

    assert backend_calls == []


@pytest.mark.parametrize(
    ("credential_field", "replacement"),
    (
        ("backend_url", "https://other-backend.example"),
        ("instance_id", "inst-new"),
        ("instance_secret", "replacement-secret"),
    ),
)
@pytest.mark.parametrize("operation", ("get", "authorized_users", "project_access"))
def test_permissions_rejects_inflight_results_after_the_pairing_tuple_changes(
    monkeypatch,
    credential_field: str,
    replacement: str,
    operation: str,
) -> None:
    config = _config()
    cached_projection = _complete_projection()
    permissions._cache_projection("inst-123", cached_projection)  # noqa: SLF001
    backend_projection = _complete_projection()
    backend_projection["instance"]["authorization_revision"] = 4
    backend_projection["access"]["entries"] = [
        {"kind": "email", "value": "new@example.com", "role": "editor"}
    ]
    project = backend_projection["projects"][0]
    parsed = []
    acknowledged = []

    class TrackingResponse(_Response):
        def json(self):
            parsed.append(True)
            return super().json()

    def request(_method, _url, **_kwargs):
        current = V2Config.load()
        setattr(current.remote_access.vibe_cloud, credential_field, replacement)
        current.save()
        if operation == "get":
            payload = backend_projection
        elif operation == "authorized_users":
            payload = {
                "ok": True,
                "entries": backend_projection["access"]["entries"],
                "authorization_revision": 4,
            }
        else:
            payload = {
                "ok": True,
                "project": project,
                "authorization_revision": 4,
            }
        return TrackingResponse(200, payload)

    monkeypatch.setattr(permissions.requests, "request", request)
    monkeypatch.setattr(
        permissions,
        "_acknowledge_authorization_revision",
        lambda *_args: acknowledged.append(True),
    )

    with pytest.raises(
        permissions.PermissionsPairingChangedError,
        match="permissions_pairing_changed",
    ):
        if operation == "get":
            permissions.get_current_permissions(config)
        elif operation == "authorized_users":
            permissions.replace_authorized_users(
                {
                    "entries": [],
                    "if_match_revision": 3,
                    "if_match_instance_id": "inst-123",
                },
                config,
            )
        else:
            permissions.update_project_access(
                "project-1",
                {
                    "mode": "restricted",
                    "bindings": project["access"]["bindings"],
                    "if_match_revision": 2,
                    "if_match_instance_id": "inst-123",
                },
                config,
            )

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.projection == cached_projection
    assert parsed == []
    assert acknowledged == []


def test_permissions_rejects_cached_fallback_after_inflight_pairing_change(
    monkeypatch,
) -> None:
    config = _config()
    cached_projection = _complete_projection()
    permissions._cache_projection("inst-123", cached_projection)  # noqa: SLF001

    def request(_method, _url, **_kwargs):
        current = V2Config.load()
        current.remote_access.vibe_cloud.instance_id = "inst-new"
        current.save()
        raise requests.ConnectionError()

    monkeypatch.setattr(permissions.requests, "request", request)

    with pytest.raises(
        permissions.PermissionsPairingChangedError,
        match="permissions_pairing_changed",
    ):
        permissions.get_current_permissions(config)


def test_permissions_rechecks_pairing_after_loading_cached_fallback(
    monkeypatch,
) -> None:
    config = _config()
    cached_projection = _complete_projection()
    permissions._write_cache("inst-123", cached_projection)  # noqa: SLF001
    original_read = permissions._read_cache  # noqa: SLF001
    read_count = 0

    def read_cache(instance_id: str):
        nonlocal read_count
        read_count += 1
        cached = original_read(instance_id)
        if read_count == 2:
            replacement = V2Config.load()
            replacement.remote_access.vibe_cloud.instance_id = "inst-new"
            replacement.save()
        return cached

    monkeypatch.setattr(permissions, "_read_cache", read_cache)
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError()),
    )

    with pytest.raises(
        permissions.PermissionsPairingChangedError,
        match="permissions_pairing_changed",
    ):
        permissions.get_current_permissions(config)

    assert read_count >= 2


@pytest.mark.parametrize("operation", ["get", "authorized_users", "project_access"])
def test_permissions_cache_write_rechecks_pairing_for_every_projection_writer(
    monkeypatch,
    operation: str,
) -> None:
    config = _config()
    old_projection = _complete_projection()
    new_projection = _complete_projection("inst-new")
    permissions._cache_projection("inst-123", old_projection)  # noqa: SLF001
    switched = False

    def switch_pairing() -> None:
        nonlocal switched
        if switched:
            return
        switched = True
        replacement = V2Config.load()
        replacement.remote_access.vibe_cloud.instance_id = "inst-new"
        replacement.save()
        permissions._cache_projection(  # noqa: SLF001
            "inst-new",
            new_projection,
        )

    if operation == "get":
        original_guard = permissions._guard_current_pairing  # noqa: SLF001

        def switch_after_response_guard(credentials, load_current_config):
            original_guard(credentials, load_current_config)
            switch_pairing()

        monkeypatch.setattr(
            permissions,
            "_guard_current_pairing",
            switch_after_response_guard,
        )
    else:
        monkeypatch.setattr(
            permissions,
            "_acknowledge_authorization_revision",
            lambda *_args: switch_pairing(),
        )

    def request(_method, _url, **_kwargs):
        if operation == "get":
            return _Response(200, old_projection)
        if operation == "authorized_users":
            return _Response(
                200,
                {
                    "ok": True,
                    "entries": old_projection["access"]["entries"],
                    "authorization_revision": 3,
                },
            )
        return _Response(
            200,
            {
                "ok": True,
                "project": old_projection["projects"][0],
                "authorization_revision": 3,
            },
        )

    monkeypatch.setattr(permissions.requests, "request", request)

    with pytest.raises(
        permissions.PermissionsPairingChangedError,
        match="permissions_pairing_changed",
    ):
        if operation == "get":
            permissions.get_current_permissions(config)
        elif operation == "authorized_users":
            permissions.replace_authorized_users(
                {
                    "entries": old_projection["access"]["entries"],
                    "if_match_revision": 3,
                    "if_match_instance_id": "inst-123",
                },
                config,
            )
        else:
            permissions.update_project_access(
                "project-1",
                {
                    "mode": old_projection["projects"][0]["access"]["mode"],
                    "bindings": old_projection["projects"][0]["access"]["bindings"],
                    "if_match_revision": 2,
                    "if_match_instance_id": "inst-123",
                },
                config,
            )

    cached = permissions._read_cache("inst-new")  # noqa: SLF001
    assert cached is not None
    assert cached.projection == new_projection
    assert permissions._read_cache("inst-123") is None  # noqa: SLF001


@pytest.mark.parametrize(
    "load_error",
    [
        pytest.param(OSError("config unavailable"), id="io-error"),
        pytest.param(TypeError("config shape changed"), id="type-error"),
        pytest.param(ValueError("config value changed"), id="value-error"),
    ],
)
def test_permissions_fails_closed_when_pairing_revalidation_cannot_load_config(
    monkeypatch,
    load_error: Exception,
) -> None:
    config = _config()
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, _complete_projection()),
    )

    def fail_load(_cls, *_args, **_kwargs):
        raise load_error

    monkeypatch.setattr(V2Config, "load", classmethod(fail_load))

    with pytest.raises(
        permissions.PermissionsPairingChangedError,
        match="permissions_pairing_changed",
    ):
        permissions.get_current_permissions(config)

    assert not permissions._cache_path().exists()  # noqa: SLF001


@pytest.mark.parametrize(("path", "replacement"), MALFORMED_PROJECTION_CASES)
def test_permissions_rejects_each_malformed_nested_projection_before_caching(
    monkeypatch,
    path: tuple[str | int, ...],
    replacement: object,
) -> None:
    malformed = _complete_projection()
    _replace_nested(malformed, path, replacement)
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, malformed),
    )

    with pytest.raises(permissions.PermissionsInvalidResponseError):
        permissions.get_current_permissions(_config())

    assert not permissions._cache_path().exists()  # noqa: SLF001


@pytest.mark.parametrize("path", PROJECTION_IDENTITY_COLLECTION_PATHS)
@pytest.mark.parametrize("source", ["live", "cache"])
def test_permissions_rejects_duplicate_projection_identities_at_the_shared_boundary(
    monkeypatch,
    path: tuple[str | int, ...],
    source: str,
) -> None:
    malformed = _complete_projection()
    collection: Any = malformed
    for key in path:
        collection = collection[key]
    collection.append(deepcopy(collection[0]))

    if source == "live":
        monkeypatch.setattr(
            permissions.requests,
            "request",
            lambda *_args, **_kwargs: _Response(200, malformed),
        )
        expected_error = permissions.PermissionsInvalidResponseError
    else:
        permissions._write_cache("inst-123", malformed)  # noqa: SLF001
        monkeypatch.setattr(
            permissions.requests,
            "request",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError()),
        )
        expected_error = permissions.PermissionsUnavailableError

    with pytest.raises(expected_error):
        permissions.get_current_permissions(_config())

    assert permissions._read_cache("inst-123") is None  # noqa: SLF001


@pytest.mark.parametrize("path", NONEMPTY_PROJECTION_STRING_PATHS)
@pytest.mark.parametrize("replacement", ["", " \t"], ids=["empty", "whitespace"])
def test_permissions_rejects_blank_projection_identifiers_and_principals(
    monkeypatch,
    path: tuple[str | int, ...],
    replacement: str,
) -> None:
    malformed = _complete_projection()
    _replace_nested(malformed, path, replacement)
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, malformed),
    )

    with pytest.raises(permissions.PermissionsInvalidResponseError):
        permissions.get_current_permissions(_config())

    assert not permissions._cache_path().exists()  # noqa: SLF001


@pytest.mark.parametrize("instance_id", ["", " \t"], ids=["empty", "whitespace"])
def test_permissions_rejects_a_blank_matching_instance_identifier(instance_id: str) -> None:
    with pytest.raises(permissions.PermissionsInvalidResponseError):
        permissions._validated_projection(  # noqa: SLF001
            _complete_projection(instance_id),
            instance_id,
        )


@pytest.mark.parametrize("project_id", INVALID_PROJECT_ROUTE_IDS)
def test_permissions_rejects_project_route_ids_before_caching(
    monkeypatch,
    project_id: str,
) -> None:
    malformed = _complete_projection()
    malformed["projects"][0]["project_id"] = project_id
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, malformed),
    )

    with pytest.raises(permissions.PermissionsInvalidResponseError):
        permissions.get_current_permissions(_config())

    assert not permissions._cache_path().exists()  # noqa: SLF001


@pytest.mark.parametrize("project_id", INVALID_PROJECT_ROUTE_IDS)
def test_permissions_ignores_cached_project_route_ids(
    monkeypatch,
    project_id: str,
) -> None:
    malformed = _complete_projection()
    malformed["projects"][0]["project_id"] = project_id
    permissions._write_cache("inst-123", malformed)  # noqa: SLF001
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError()),
    )

    with pytest.raises(permissions.PermissionsUnavailableError):
        permissions.get_current_permissions(_config())

    assert permissions._read_cache("inst-123") is None  # noqa: SLF001


@pytest.mark.parametrize("project_id", INVALID_PROJECT_ROUTE_IDS)
def test_permissions_rejects_project_route_ids_before_mutation(
    monkeypatch,
    project_id: str,
) -> None:
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: pytest.fail("invalid Project ID reached Backend"),
    )

    with pytest.raises(
        permissions.PermissionsInvalidResponseError,
        match="invalid_project_id",
    ):
        permissions.update_project_access(
            project_id,
            {
                "mode": "inherit",
                "bindings": [],
                "if_match_revision": 2,
                "if_match_instance_id": "inst-123",
            },
            _config(),
        )


@pytest.mark.parametrize("project_id", INVALID_PROJECT_ROUTE_IDS)
def test_permissions_rejects_project_route_ids_in_mutation_results(
    project_id: str,
) -> None:
    valid = _complete_projection()
    malformed_project = {
        **valid["projects"][0],
        "project_id": project_id,
    }

    with pytest.raises(permissions.PermissionsInvalidResponseError):
        permissions._validated_project_result(  # noqa: SLF001
            {
                "ok": True,
                "project": malformed_project,
                "authorization_revision": 4,
            },
            project_id,
        )


def test_permissions_preserves_additive_backend_capabilities_live_and_offline(
    monkeypatch,
) -> None:
    projection = _complete_projection()
    projection["capabilities"].append("instance.permissions.audit")
    backend_available = True

    def request(*_args, **_kwargs):
        if not backend_available:
            raise requests.ConnectionError()
        return _Response(200, projection)

    monkeypatch.setattr(permissions.requests, "request", request)
    live = permissions.get_current_permissions(_config())
    backend_available = False
    cached = permissions.get_current_permissions(_config())

    assert live.projection["capabilities"] == projection["capabilities"]
    assert cached.projection["capabilities"] == projection["capabilities"]


def test_permissions_preserves_nullable_projection_fields(monkeypatch) -> None:
    projection = _complete_projection()
    projection["access"]["owner"]["email"] = None
    projection["projects"][0]["organization_id"] = None
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(200, projection),
    )

    result = permissions.get_current_permissions(_config())

    assert result.projection["access"]["owner"]["email"] is None
    assert result.projection["projects"][0]["organization_id"] is None


def test_permissions_ignores_a_malformed_offline_cache(monkeypatch) -> None:
    malformed = _complete_projection()
    malformed["projects"][0]["sync"] = None
    permissions._write_cache("inst-123", malformed)  # noqa: SLF001
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError()),
    )

    with pytest.raises(permissions.PermissionsUnavailableError):
        permissions.get_current_permissions(_config())


def test_permissions_ignores_blank_identifiers_in_the_offline_cache(monkeypatch) -> None:
    malformed = _complete_projection()
    malformed["projects"][0]["project_id"] = " \t"
    permissions._write_cache("inst-123", malformed)  # noqa: SLF001
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError()),
    )

    with pytest.raises(permissions.PermissionsUnavailableError):
        permissions.get_current_permissions(_config())


def test_invalid_mutation_result_cannot_replace_the_valid_cache(monkeypatch) -> None:
    live = _complete_projection()

    def request(method, _url, **_kwargs):
        if method == "GET":
            return _Response(200, live)
        return _Response(
            200,
            {
                "ok": True,
                "entries": [{"kind": "email", "value": "bad@example.com", "role": "owner"}],
                "authorization_revision": 4,
            },
        )

    monkeypatch.setattr(permissions.requests, "request", request)
    config = _config()
    permissions.get_current_permissions(config)

    with pytest.raises(permissions.PermissionsInvalidResponseError):
        permissions.replace_authorized_users(
            {
                "entries": [],
                "if_match_revision": 3,
                "if_match_instance_id": "inst-123",
            },
            config,
        )

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.projection == live


def test_mutation_result_is_sanitized_before_cache_write(monkeypatch) -> None:
    live = _projection()
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
                "authorization_revision": 4,
                "entries": [
                    {
                        "kind": "email",
                        "value": "new@example.com",
                        "role": "editor",
                        "future": "preserved",
                        "api_token": "must-not-persist",
                    }
                ],
            },
        )

    monkeypatch.setattr(permissions.requests, "request", request)
    config = _config()
    permissions.get_current_permissions(config)

    result = permissions.replace_authorized_users(
        {
            "entries": [],
            "if_match_revision": 3,
            "if_match_instance_id": "inst-123",
        },
        config,
    )
    backend_available = False
    cached = permissions.get_current_permissions(config)

    assert result["entries"][0]["future"] == "preserved"
    assert "api_token" not in result["entries"][0]
    assert cached.projection["access"]["entries"] == result["entries"]


def test_older_mutation_result_replays_its_entity_without_lowering_cache_revision(monkeypatch) -> None:
    live = _complete_projection()
    live["instance"]["authorization_revision"] = 5
    backend_available = True
    stale_entries = [{"kind": "email", "value": "stale@example.com", "role": "viewer"}]

    def request(method, _url, **_kwargs):
        if method == "GET":
            if not backend_available:
                raise requests.ConnectionError()
            return _Response(200, live)
        return _Response(
            200,
            {
                "ok": True,
                "entries": stale_entries,
                "authorization_revision": 4,
            },
        )

    monkeypatch.setattr(permissions.requests, "request", request)
    config = _config()
    permissions.get_current_permissions(config)
    permissions.replace_authorized_users(
        {
            "entries": stale_entries,
            "if_match_revision": 3,
            "if_match_instance_id": "inst-123",
        },
        config,
    )
    backend_available = False

    cached = permissions.get_current_permissions(config)

    assert cached.projection["instance"]["authorization_revision"] == 5
    assert cached.projection["access"]["entries"] == stale_entries


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
        {
            "entries": updated_entries,
            "if_match_revision": 3,
            "if_match_instance_id": "inst-123",
        },
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
        "access": {"mode": "inherit", "revision": 1, "bindings": []},
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
        "access": {"mode": "restricted", "revision": 2, "bindings": []},
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
        {
            "mode": "restricted",
            "bindings": [],
            "if_match_revision": 1,
            "if_match_instance_id": "inst-123",
        },
        config,
    )
    backend_available = False
    cached = permissions.get_current_permissions(config)

    assert cached.source == "cache"
    assert cached.projection["instance"]["authorization_revision"] == 4
    assert cached.projection["projects"] == [updated_project]


def test_permissions_mutations_advance_and_publish_the_authorization_watermark(
    monkeypatch,
) -> None:
    project = _complete_projection()["projects"][0]
    published = []

    def request(_method, url, **_kwargs):
        if url.endswith("/authorized-users"):
            return _Response(
                200,
                {
                    "ok": True,
                    "entries": [],
                    "authorization_revision": 4,
                },
            )
        return _Response(
            200,
            {
                "ok": True,
                "project": project,
                "authorization_revision": 5,
            },
        )

    monkeypatch.setattr(permissions.requests, "request", request)
    monkeypatch.setattr(
        broker,
        "publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    remote_access._clear_authorization_revision_cache()  # noqa: SLF001
    config = _config()

    permissions.replace_authorized_users(
        {
            "entries": [],
            "if_match_revision": 3,
            "if_match_instance_id": "inst-123",
        },
        config,
    )
    permissions.update_project_access(
        "project-1",
        {
            "mode": "restricted",
            "bindings": project["access"]["bindings"],
            "if_match_revision": 2,
            "if_match_instance_id": "inst-123",
        },
        config,
    )

    assert remote_access.current_authorization_revision(config) == 5
    assert published == [
        ("authorization.changed", {"instance_authorization_revision": 4}),
        ("authorization.changed", {"instance_authorization_revision": 5}),
    ]


def test_permissions_mutation_surfaces_pairing_change_during_watermark_ack(
    monkeypatch,
) -> None:
    projection = _complete_projection()
    monkeypatch.setattr(
        permissions.requests,
        "request",
        lambda *_args, **_kwargs: _Response(
            200,
            {
                "ok": True,
                "entries": [],
                "authorization_revision": 4,
            },
        ),
    )
    monkeypatch.setattr(
        remote_access,
        "acknowledge_authorization_revision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            remote_access.AuthorizationRevisionPairingChangedError(
                "authorization_revision_pairing_changed"
            )
        ),
    )

    permissions._cache_projection("inst-123", projection)  # noqa: SLF001
    with pytest.raises(
        permissions.PermissionsPairingChangedError,
        match="permissions_pairing_changed",
    ):
        permissions.replace_authorized_users(
            {
                "entries": [],
                "if_match_revision": 3,
                "if_match_instance_id": "inst-123",
            },
            _config(),
        )

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    assert cached is not None
    assert cached.projection == projection


@pytest.mark.parametrize("operation", ("authorized_users", "project_access"))
@pytest.mark.parametrize("persistence_failure", ("write_error", "lock_timeout"))
def test_committed_permissions_mutations_survive_watermark_persistence_failure(
    monkeypatch,
    operation: str,
    persistence_failure: str,
) -> None:
    config = _config()
    projection = _complete_projection()
    permissions._cache_projection("inst-123", projection)  # noqa: SLF001
    remote_access._clear_authorization_revision_cache()  # noqa: SLF001
    remote_access._replace_authorization_revision(config, 3)  # noqa: SLF001
    published = []
    monkeypatch.setattr(
        broker,
        "publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    if persistence_failure == "write_error":
        monkeypatch.setattr(
            remote_access.runtime,
            "write_json",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only state")),
        )
    else:
        def timed_out_lock(_path):
            raise MigrationLockTimeout("watermark lock timed out")

        monkeypatch.setattr(
            remote_access,
            "_authorization_revision_file_lock",
            timed_out_lock,
        )
    updated_entries = [
        {"kind": "email", "value": "new@example.com", "role": "editor"}
    ]
    updated_project = {
        **projection["projects"][0],
        "access": {
            **projection["projects"][0]["access"],
            "revision": 3,
        },
    }

    def request(_method, url, **_kwargs):
        if url.endswith("/authorized-users"):
            return _Response(
                200,
                {
                    "ok": True,
                    "entries": updated_entries,
                    "authorization_revision": 4,
                },
            )
        return _Response(
            200,
            {
                "ok": True,
                "project": updated_project,
                "authorization_revision": 4,
            },
        )

    monkeypatch.setattr(permissions.requests, "request", request)

    if operation == "authorized_users":
        result = permissions.replace_authorized_users(
            {
                "entries": updated_entries,
                "if_match_revision": 3,
                "if_match_instance_id": "inst-123",
            },
            config,
        )
    else:
        result = permissions.update_project_access(
            "project-1",
            {
                "mode": "restricted",
                "bindings": updated_project["access"]["bindings"],
                "if_match_revision": 2,
                "if_match_instance_id": "inst-123",
            },
            config,
        )

    cached = permissions._read_cache("inst-123")  # noqa: SLF001
    persisted = json.loads(
        remote_access._authorization_revision_state_path().read_text(encoding="utf-8")  # noqa: SLF001
    )
    assert result["authorization_revision"] == 4
    assert result["instance_id"] == "inst-123"
    assert remote_access.current_authorization_revision(config) == 4
    assert persisted["authorization_revision"] == 3
    assert remote_access.acknowledge_authorization_revision(config, 4) == 4
    assert remote_access.acknowledge_authorization_revision(config, 3) == 4
    assert remote_access.current_authorization_revision(config) == 4
    assert published == [
        ("authorization.changed", {"instance_authorization_revision": 4})
    ]
    assert cached is not None
    assert cached.projection["instance"]["authorization_revision"] == 4
    if operation == "authorized_users":
        assert cached.projection["access"]["entries"] == updated_entries
    else:
        assert cached.projection["projects"] == [updated_project]


def test_out_of_order_mutation_acknowledgement_keeps_the_newer_watermark_epoch(
    monkeypatch,
) -> None:
    source_times = iter((100.0, 200.0))
    monkeypatch.setattr(remote_access.time, "time", lambda: next(source_times))
    remote_access._clear_authorization_revision_cache()  # noqa: SLF001
    config = _config()

    remote_access._replace_authorization_revision(config, 5)  # noqa: SLF001
    assert remote_access.acknowledge_authorization_revision(config, 4) == 5

    assert remote_access._load_authorization_revision_snapshot(config) == (  # noqa: SLF001
        5,
        100.0,
    )


def test_permissions_http_policy_allows_viewer_reads_but_owner_only_mutations() -> None:
    assert http_authorization_policy("GET", "/api/permissions").minimum_role == "viewer"
    assert (
        http_authorization_policy("PUT", "/api/permissions/authorized-users").minimum_role
        == "owner"
    )
    assert (
        http_authorization_policy("PUT", "/api/permissions/projects/project-1/access").minimum_role
        == "member"
    )
    assert (
        http_authorization_policy(
            "GET", "/api/permissions/resources/agent/agent-1/access"
        ).minimum_role
        == "viewer"
    )
    assert (
        http_authorization_policy(
            "PUT", "/api/permissions/resources/agent/agent-1/access"
        ).minimum_role
        == "member"
    )


def test_permissions_projection_get_is_private_and_not_cached(monkeypatch) -> None:
    client = app.test_client()
    monkeypatch.setattr(
        permissions,
        "get_current_permissions",
        lambda: permissions.PermissionsProjectionResult(
            projection=_projection(),
            source="live",
        ),
    )

    response = client.get("/api/permissions")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.get_json()["projection"]["instance"]["id"] == "inst-123"


def test_permissions_projection_rejects_page_scoped_guest_before_backend(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    backend_called = False

    def get_current_permissions():
        nonlocal backend_called
        backend_called = True
        return permissions.PermissionsProjectionResult(
            projection=_projection(),
            source="live",
        )

    monkeypatch.setattr(permissions, "get_current_permissions", get_current_permissions)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "guest@example.com",
            "guest-1",
            session_claims={
                "vibe_instance_id": config.remote_access.vibe_cloud.instance_id,
                "vibe_instance_role": "viewer",
                "vibe_instance_access_source": "show_page_email",
                "vibe_show_page_id": "session-one",
            },
        ),
        domain="alex.avibe.bot",
    )

    response = client.get(
        "/api/permissions",
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "ok": False,
        "error": "show_page_access_forbidden",
    }
    assert backend_called is False


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
            "if_match_instance_id": "inst-123",
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
        json={
            "mode": "inherit",
            "bindings": [],
            "if_match_revision": 6,
            "if_match_instance_id": "inst-123",
        },
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
        "payload": {
            "mode": "inherit",
            "bindings": [],
            "if_match_revision": 6,
            "if_match_instance_id": "inst-123",
        },
    }


def test_permissions_same_origin_route_surfaces_a_changed_pairing(monkeypatch) -> None:
    client = app.test_client()
    headers = csrf_headers(client)

    def replace(_payload):
        raise permissions.PermissionsPairingChangedError("permissions_pairing_changed")

    monkeypatch.setattr(permissions, "replace_authorized_users", replace)
    response = client.put(
        "/api/permissions/authorized-users",
        json={
            "entries": [],
            "if_match_revision": 3,
            "if_match_instance_id": "inst-old",
        },
        headers=headers,
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "ok": False,
        "error": "permissions_pairing_changed",
    }


def test_member_can_manage_instance_settings_but_not_authorized_users(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    authorized_users_called = False
    project_access_called = False

    def replace(_payload):
        nonlocal authorized_users_called
        authorized_users_called = True
        return {"ok": True, "instance_id": "inst-123", "entries": [], "authorization_revision": 4}

    def update(_project_id, _payload):
        nonlocal project_access_called
        project_access_called = True
        return {
            "ok": True,
            "instance_id": "inst-123",
            "project": _complete_projection()["projects"][0],
            "authorization_revision": 4,
        }

    monkeypatch.setattr(permissions, "replace_authorized_users", replace)
    monkeypatch.setattr(permissions, "update_project_access", update)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "member@example.com",
            "member-1",
            role="member",
            access_source="email",
        ),
        domain="alex.avibe.bot",
    )
    headers = csrf_headers(client, base_url="https://alex.avibe.bot")

    project_response = client.put(
        "/api/permissions/projects/project-1/access",
        json={
            "mode": "inherit",
            "bindings": [],
            "if_match_revision": 3,
            "if_match_instance_id": "inst-123",
        },
        headers=headers,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )
    users_response = client.put(
        "/api/permissions/authorized-users",
        json={
            "entries": [],
            "if_match_revision": 3,
            "if_match_instance_id": "inst-123",
        },
        headers=headers,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )

    assert project_response.status_code == 200
    assert project_access_called is True
    assert users_response.status_code == 403
    assert users_response.get_json()["error"] == "instance_access_forbidden"
    assert authorized_users_called is False


def test_owner_can_mutate_authorized_users(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    called = False

    def replace(payload):
        nonlocal called
        called = True
        assert payload["entries"] == [
            {"kind": "email", "value": "member@example.com", "role": "member"}
        ]
        return {
            "ok": True,
            "instance_id": "inst-123",
            "entries": payload["entries"],
            "authorization_revision": 5,
        }

    monkeypatch.setattr(permissions, "replace_authorized_users", replace)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "owner@example.com",
            "owner-1",
            role="owner",
            access_source="owner",
        ),
        domain="alex.avibe.bot",
    )
    headers = csrf_headers(client, base_url="https://alex.avibe.bot")
    response = client.put(
        "/api/permissions/authorized-users",
        json={
            "entries": [
                {"kind": "email", "value": "member@example.com", "role": "member"}
            ],
            "if_match_revision": 4,
            "if_match_instance_id": "inst-123",
        },
        headers=headers,
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )

    assert response.status_code == 200
    assert called is True


def test_resource_access_same_origin_routes_forward_exact_identity_and_conflict(
    monkeypatch,
) -> None:
    client = app.test_client()
    headers = csrf_headers(client)
    captured = []

    def get_resource(resource_kind, resource_id):
        captured.append(("GET", resource_kind, resource_id, None))
        return {"resource": _resource(resource_id=resource_id)}

    def update_resource(resource_kind, resource_id, payload):
        captured.append(("PUT", resource_kind, resource_id, payload))
        raise permissions.PermissionsBackendError(
            409,
            {"error": "permission_revision_conflict", "current_revision": 7},
        )

    monkeypatch.setattr(permissions, "get_resource_access", get_resource)
    monkeypatch.setattr(permissions, "update_resource_access", update_resource)
    read = client.get("/api/permissions/resources/agent/agent-1/access")
    write = client.put(
        "/api/permissions/resources/agent/agent-1/access",
        json={
            "access_level": "scope",
            "group_ids": ["group-1"],
            "if_match_revision": 4,
            "if_match_instance_id": "inst-123",
        },
        headers=headers,
    )

    assert read.status_code == 200
    assert read.headers["Cache-Control"] == "private, no-store"
    assert read.get_json()["resource"]["resource_id"] == "agent-1"
    assert write.status_code == 409
    assert write.get_json() == {
        "ok": False,
        "error": "permission_revision_conflict",
        "current_revision": 7,
    }
    assert captured == [
        ("GET", "agent", "agent-1", None),
        (
            "PUT",
            "agent",
            "agent-1",
            {
                "access_level": "scope",
                "group_ids": ["group-1"],
                "if_match_revision": 4,
                "if_match_instance_id": "inst-123",
            },
        ),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "access_level": "scope",
            "group_ids": [],
            "if_match_revision": 4,
            "if_match_instance_id": "inst-123",
        },
        {
            "access_level": "private",
            "group_ids": ["group-1"],
            "if_match_revision": 4,
            "if_match_instance_id": "inst-123",
        },
        {
            "access_level": "scope",
            "group_ids": ["group-1", "group-1"],
            "if_match_revision": 4,
            "if_match_instance_id": "inst-123",
        },
        {
            "access_level": "scope",
            "group_ids": ["group-1"],
            "if_match_revision": True,
            "if_match_instance_id": "inst-123",
        },
    ],
)
def test_resource_access_same_origin_route_rejects_invalid_payload_before_backend(
    monkeypatch,
    payload,
) -> None:
    called = False

    def update_resource(*_args):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(permissions, "update_resource_access", update_resource)
    client = app.test_client()
    response = client.put(
        "/api/permissions/resources/agent/agent-1/access",
        json=payload,
        headers=csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.get_json() == {"ok": False, "error": "invalid_request"}
    assert called is False


def test_resource_access_route_rejects_page_scoped_guest_before_backend(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    backend_called = False

    def get_resource(*_args):
        nonlocal backend_called
        backend_called = True
        return {"resource": _resource()}

    monkeypatch.setattr(permissions, "get_resource_access", get_resource)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "guest@example.com",
            "guest-1",
            session_claims={
                "vibe_instance_id": config.remote_access.vibe_cloud.instance_id,
                "vibe_instance_role": "viewer",
                "vibe_instance_access_source": "show_page_email",
                "vibe_show_page_id": "agent-1",
            },
        ),
        domain="alex.avibe.bot",
    )

    response = client.get(
        "/api/permissions/resources/agent/agent-1/access",
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "ok": False,
        "error": "show_page_access_forbidden",
    }
    assert backend_called is False


def test_resource_access_route_surfaces_cloud_authority_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        permissions,
        "update_resource_access",
        lambda *_args: (_ for _ in ()).throw(
            permissions.PermissionsBackendError(
                403,
                {"error": "permission_authority_cloud"},
            )
        ),
    )
    client = app.test_client()
    response = client.put(
        "/api/permissions/resources/agent/agent-1/access",
        json={
            "access_level": "private",
            "group_ids": [],
            "if_match_revision": 4,
            "if_match_instance_id": "inst-123",
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "ok": False,
        "error": "permission_authority_cloud",
    }
