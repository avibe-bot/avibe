from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.scenario_harness.permissions import PermissionsScenarioHarness, REMOTE_ORIGIN
from vibe.ui_server import app


@pytest.fixture
def harness():
    value = PermissionsScenarioHarness()
    try:
        yield value
    finally:
        value.close()


def test_permissions_001_002_existing_sessions_enter_without_management_oauth(harness) -> None:
    """Scenarios: PERMISSIONS-001, PERMISSIONS-002."""
    local = harness.local_client().get("/api/permissions")
    remote = harness.remote_client("owner").get(
        "/api/permissions",
        base_url=REMOTE_ORIGIN,
    )

    assert local.status_code == 200
    assert remote.status_code == 200
    assert local.get_json()["projection"]["instance"]["id"] == "inst-current"
    assert remote.get_json()["projection"]["instance"]["id"] == "inst-current"
    assert all("oauth" not in request["url"] for request in harness.backend_requests)


def test_permissions_003_viewer_mutation_fails_before_backend_contact(harness) -> None:
    """Scenario: PERMISSIONS-003."""
    viewer = harness.remote_client("viewer")
    read = viewer.get("/api/permissions", base_url=REMOTE_ORIGIN)
    before = len(harness.backend_requests)
    write = viewer.put(
        "/api/permissions/authorized-users",
        json={
            "entries": [],
            "if_match_revision": 0,
            "if_match_instance_id": "inst-current",
        },
        headers=harness.csrf(viewer, REMOTE_ORIGIN),
        base_url=REMOTE_ORIGIN,
    )

    assert read.status_code == 200
    assert write.status_code == 403
    assert write.get_json()["error"] == "instance_access_forbidden"
    assert len(harness.backend_requests) == before


def test_permissions_004_instance_managed_write_advances_revision(harness) -> None:
    """Scenario: PERMISSIONS-004."""
    client = harness.local_client()
    response = client.put(
        "/api/permissions/authorized-users",
        json={
            "entries": [{"kind": "email", "value": "member@example.com", "role": "editor"}],
            "if_match_revision": 0,
            "if_match_instance_id": "inst-current",
        },
        headers=harness.csrf(client),
    )

    assert response.status_code == 200
    assert response.get_json()["instance_id"] == "inst-current"
    assert response.get_json()["authorization_revision"] == 1
    assert harness.projection["access"]["entries"][0]["role"] == "editor"
    assert harness.projection["instance"]["authorization_revision"] == 1
    assert "if_match_instance_id" not in harness.backend_requests[-1]["json"]


def test_permissions_005_cloud_authority_is_readable_and_read_only(harness) -> None:
    """Scenario: PERMISSIONS-005."""
    harness.projection["instance"].update(
        {"permission_authority": "cloud", "local_mutation_allowed": False}
    )
    harness.projection["capabilities"] = ["instance.permissions.read"]
    client = harness.local_client()
    read = client.get("/api/permissions")
    write = client.put(
        "/api/permissions/authorized-users",
        json={
            "entries": [],
            "if_match_revision": 0,
            "if_match_instance_id": "inst-current",
        },
        headers=harness.csrf(client),
    )

    assert read.status_code == 200
    assert read.get_json()["projection"]["instance"]["permission_authority"] == "cloud"
    assert write.status_code == 403
    assert write.get_json()["error"] == "permission_authority_cloud"


def test_permissions_006_offline_uses_bound_cached_projection(harness) -> None:
    """Scenario: PERMISSIONS-006."""
    client = harness.local_client()
    live = client.get("/api/permissions")
    harness.backend_available = False
    cached = client.get("/api/permissions")

    assert live.get_json()["source"] == "live"
    assert cached.status_code == 200
    assert cached.get_json()["source"] == "cache"
    assert cached.get_json()["offline"] is True
    assert cached.get_json()["projection"] == live.get_json()["projection"]


def test_permissions_007_project_write_applies_then_acknowledges(harness) -> None:
    """Scenario: PERMISSIONS-007."""
    client = harness.local_client()
    write = client.put(
        "/api/permissions/projects/project-1/access",
        json={
            "mode": "restricted",
            "bindings": [
                {
                    "principal_kind": "organization_group",
                    "principal_value": "group-1",
                    "access_role": "viewer",
                }
            ],
            "if_match_revision": 0,
            "if_match_instance_id": "inst-current",
        },
        headers=harness.csrf(client),
    )
    assert write.status_code == 200
    assert write.get_json()["instance_id"] == "inst-current"
    assert write.get_json()["project"]["sync"]["status"] == "pending"
    assert write.get_json()["authorization_revision"] == 1

    harness.acknowledge_project()
    current = client.get("/api/permissions")
    assert current.get_json()["projection"]["projects"][0]["sync"]["status"] == "in_sync"
    assert current.get_json()["projection"]["projects"][0]["sync"]["applied_access_revision"] == 1


def test_permissions_008_conflict_is_stable_and_non_mutating(harness) -> None:
    """Scenario: PERMISSIONS-008."""
    harness.projection["instance"]["authorization_revision"] = 4
    client = harness.local_client()
    response = client.put(
        "/api/permissions/authorized-users",
        json={
            "entries": [{"kind": "email", "value": "stale@example.com", "role": "viewer"}],
            "if_match_revision": 3,
            "if_match_instance_id": "inst-current",
        },
        headers=harness.csrf(client),
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "permission_revision_conflict"
    assert response.get_json()["current_revision"] == 4
    assert harness.projection["access"]["entries"] == []


def test_permissions_009_targeting_and_stale_pairing_are_rejected_before_backend(harness) -> None:
    """Scenario: PERMISSIONS-009."""
    client = harness.local_client()
    before = len(harness.backend_requests)
    response = client.put(
        "/api/permissions/authorized-users",
        json={
            "entries": [
                {
                    "kind": "email",
                    "value": "member@example.com",
                    "role": "viewer",
                    "instance_id": "inst-other",
                }
            ],
            "if_match_revision": 0,
            "if_match_instance_id": "inst-current",
        },
        headers=harness.csrf(client),
    )

    assert response.status_code == 422
    assert response.get_json()["error"] == "invalid_request"
    assert len(harness.backend_requests) == before

    harness.config.remote_access.vibe_cloud.instance_id = "inst-repaired"
    harness.config.remote_access.vibe_cloud.instance_secret = "repaired-device-secret"
    harness.config.save()
    stale_page = client.put(
        "/api/permissions/authorized-users",
        json={
            "entries": [],
            "if_match_revision": 0,
            "if_match_instance_id": "inst-current",
        },
        headers=harness.csrf(client),
    )

    assert stale_page.status_code == 409
    assert stale_page.get_json()["error"] == "permissions_pairing_changed"
    assert len(harness.backend_requests) == before


def test_permissions_010_legacy_management_surfaces_are_absent() -> None:
    """Scenario: PERMISSIONS-010."""
    route_paths = {route.path for route in app.routes}
    assert not any(path.startswith("/api/cloud-management") for path in route_paths)
    assert "/auth/organization/start" not in route_paths
    assert "/auth/organization/callback" not in route_paths

    root = Path(__file__).resolve().parents[3]
    assert not (root / "vibe" / "cloud_management.py").exists()
    assert not (root / "ui" / "src" / "features" / "organization").exists()
    show_control = root / "ui" / "src" / "components" / "workbench" / "ShowPageShareControl.tsx"
    source = show_control.read_text(encoding="utf-8")
    assert "cloud-management" not in source
    assert "ShowPageWorkspaceAccessControl" not in source
    assert "ShowPageSharingSettings" in source


def test_permissions_011_resource_acl_round_trip_and_conflict(harness) -> None:
    """Scenario: PERMISSIONS-011."""
    client = harness.local_client()
    resource_path = "/api/permissions/resources/agent/ses-resource/access"

    initial = client.get(resource_path)
    write = client.put(
        resource_path,
        json={
            "access_level": "scope",
            "group_ids": ["group-1"],
            "if_match_revision": 0,
            "if_match_instance_id": "inst-current",
        },
        headers=harness.csrf(client),
    )
    readback = client.get(resource_path)

    assert initial.status_code == 200
    assert initial.get_json()["resource"]["access"] == {
        "access_level": "private",
        "group_ids": [],
        "revision": 0,
    }
    assert write.status_code == 200
    assert write.get_json()["resource"]["access"] == {
        "access_level": "scope",
        "group_ids": ["group-1"],
        "revision": 1,
    }
    assert readback.status_code == 200
    assert readback.get_json()["resource"] == write.get_json()["resource"]
    backend_write = next(
        request
        for request in harness.backend_requests
        if request["method"] == "PUT" and "/permissions/resources/" in request["url"]
    )
    assert backend_write["json"] == {
        "access_level": "scope",
        "group_ids": ["group-1"],
        "if_match_revision": 0,
    }

    before_conflict = deepcopy(harness.resource)
    conflict = client.put(
        resource_path,
        json={
            "access_level": "public",
            "group_ids": [],
            "if_match_revision": 0,
            "if_match_instance_id": "inst-current",
        },
        headers=harness.csrf(client),
    )

    assert conflict.status_code == 409
    assert conflict.get_json() == {
        "ok": False,
        "error": "permission_revision_conflict",
        "current_revision": 1,
    }
    assert harness.resource == before_conflict


def test_permissions_012_show_page_resource_kind_is_retired(harness) -> None:
    """Scenario: PERMISSIONS-012."""
    client = harness.local_client()
    resource_path = "/api/permissions/resources/show_page/ses-resource/access"
    before = len(harness.backend_requests)

    read = client.get(resource_path)
    write = client.put(
        resource_path,
        json={
            "access_level": "scope",
            "group_ids": ["group-1"],
            "if_match_revision": 0,
            "if_match_instance_id": "inst-current",
        },
        headers=harness.csrf(client),
    )

    # §3.2 retired show_page from the Resource ACL: the local boundary rejects
    # the retired kind as an invalid resource before any Backend contact.
    assert read.status_code == 422
    assert read.get_json() == {"ok": False, "error": "invalid_resource_kind"}
    assert write.status_code == 422
    assert write.get_json() == {"ok": False, "error": "invalid_resource_kind"}
    assert len(harness.backend_requests) == before
