from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from config.v2_config import (
    AgentsConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from tests.ui_server_test_helpers import csrf_headers, remote_session_cookie
from vibe import permissions, remote_access
from vibe.ui_server import app


REMOTE_ORIGIN = "https://permissions.avibe.bot"


class _BackendResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = deepcopy(payload)
        self.ok = 200 <= status < 300

    def json(self) -> dict:
        return deepcopy(self._payload)


class PermissionsScenarioHarness:
    """Closed-loop browser/local-service harness for current-instance Permissions."""

    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self._environment = patch.dict(
            os.environ,
            {"AVIBE_HOME": str(Path(self._tempdir.name) / "avibe-home")},
        )
        self._environment.start()
        self.config = self._save_config()
        self.backend_available = True
        self.backend_requests: list[dict] = []
        self.projection = self._projection()
        self.resource = self._resource()
        self._backend_patch = patch.object(
            permissions.requests,
            "request",
            side_effect=self._backend_request,
        )
        self._backend_patch.start()

    def _save_config(self) -> V2Config:
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
        cloud.backend_url = "https://backend.example"
        cloud.public_url = REMOTE_ORIGIN
        cloud.client_id = "permissions-client"
        cloud.instance_id = "inst-current"
        cloud.instance_secret = "paired-device-secret"
        cloud.session_secret = "permissions-session-secret"
        cloud.issuer = "https://backend.example"
        cloud.jwks_uri = "https://backend.example/.well-known/jwks.json"
        cloud.authorization_endpoint = "https://backend.example/oauth/authorize"
        cloud.token_endpoint = "https://backend.example/oauth/token"
        cloud.redirect_uri = f"{REMOTE_ORIGIN}/auth/callback"
        config.save()
        return config

    @staticmethod
    def _projection() -> dict:
        return {
            "schema_version": 1,
            "instance": {
                "id": "inst-current",
                "access_mode": "allowlist",
                "permission_authority": "instance",
                "local_mutation_allowed": True,
                "authorization_revision": 0,
            },
            "capabilities": [
                "instance.permissions.read",
                "instance.permissions.mutate",
            ],
            "access": {
                "owner": {"email": "owner@example.com", "role": "owner"},
                "entries": [],
            },
            "directory": {
                "members": [
                    {
                        "id": "member-1",
                        "email": "member@example.com",
                        "organization_role": "member",
                        "group_ids": ["group-1"],
                    }
                ],
                "groups": [{"id": "group-1", "name": "Design", "archived_at": None}],
            },
            "projects": [
                {
                    "project_id": "project-1",
                    "organization_id": "org-1",
                    "display_name": "Launch Plan",
                    "access": {"mode": "inherit", "revision": 0, "bindings": []},
                    "sync": {
                        "status": "in_sync",
                        "desired_access_revision": 0,
                        "applied_access_revision": 0,
                        "last_synced_at": None,
                    },
                }
            ],
            "policy_sync": {
                "status": "in_sync",
                "projects": {
                    "active": 1,
                    "error": 0,
                    "offline": 0,
                    "applying": 0,
                    "in_sync": 1,
                },
                "resources": {
                    "active": 0,
                    "error": 0,
                    "offline": 0,
                    "applying": 0,
                    "in_sync": 0,
                },
            },
        }

    @staticmethod
    def _resource() -> dict:
        return {
            "instance_id": "inst-current",
            "resource_kind": "show_page",
            "resource_id": "ses-resource",
            "display_name": "Scenario Show Page",
            "owner_user_id": "subject-owner",
            "access": {
                "access_level": "private",
                "group_ids": [],
                "revision": 0,
            },
            "sync": {
                "status": "in_sync",
                "desired_acl_revision": 0,
                "applied_acl_revision": 0,
                "last_synced_at": None,
            },
        }

    def _backend_request(self, method: str, url: str, **kwargs):
        request_record = {
            "method": method,
            "url": url,
            "json": deepcopy(kwargs.get("json")),
            "headers": dict(kwargs.get("headers") or {}),
        }
        self.backend_requests.append(request_record)
        if not self.backend_available:
            raise requests.ConnectionError("scenario backend offline")
        expected_prefix = "https://backend.example/api/v1/instances/inst-current/permissions"
        if not url.startswith(expected_prefix):
            return _BackendResponse(403, {"error": "instance_credential_mismatch"})
        if request_record["headers"].get("X-Vibe-Device-Secret") != "paired-device-secret":
            return _BackendResponse(401, {"error": "invalid_device_secret"})
        path = urlsplit(url).path
        if method == "GET" and path.endswith("/permissions"):
            return _BackendResponse(200, self.projection)
        if method == "GET" and path.endswith(
            "/permissions/resources/show_page/ses-resource/access"
        ):
            return _BackendResponse(200, {"resource": self.resource})
        if self.projection["instance"]["permission_authority"] == "cloud":
            return _BackendResponse(403, {"error": "permission_authority_cloud"})
        payload = request_record["json"] or {}
        if method == "PUT" and path.endswith("/permissions/authorized-users"):
            current = self.projection["instance"]["authorization_revision"]
            if payload.get("if_match_revision") != current:
                return _BackendResponse(
                    409,
                    {"error": "permission_revision_conflict", "current_revision": current},
                )
            self.projection["access"]["entries"] = deepcopy(payload.get("entries", []))
            self.projection["instance"]["authorization_revision"] = current + 1
            return _BackendResponse(
                200,
                {
                    "ok": True,
                    "entries": self.projection["access"]["entries"],
                    "authorization_revision": current + 1,
                },
            )
        if method == "PUT" and "/permissions/projects/" in path and path.endswith("/access"):
            project = self.projection["projects"][0]
            current = project["access"]["revision"]
            if payload.get("if_match_revision") != current:
                return _BackendResponse(
                    409,
                    {"error": "permission_revision_conflict", "current_revision": current},
                )
            next_revision = current + 1
            project["access"] = {
                "mode": payload.get("mode"),
                "revision": next_revision,
                "bindings": deepcopy(payload.get("bindings", [])),
            }
            project["sync"].update(
                {
                    "status": "pending",
                    "desired_access_revision": next_revision,
                }
            )
            self.projection["policy_sync"]["status"] = "applying"
            self.projection["instance"]["authorization_revision"] += 1
            return _BackendResponse(
                200,
                {
                    "ok": True,
                    "project": project,
                    "authorization_revision": self.projection["instance"]["authorization_revision"],
                },
            )
        if method == "PUT" and path.endswith(
            "/permissions/resources/show_page/ses-resource/access"
        ):
            current = self.resource["access"]["revision"]
            if payload.get("if_match_revision") != current:
                return _BackendResponse(
                    409,
                    {"error": "permission_revision_conflict", "current_revision": current},
                )
            next_revision = current + 1
            self.resource["access"] = {
                "access_level": payload.get("access_level"),
                "group_ids": deepcopy(payload.get("group_ids", [])),
                "revision": next_revision,
            }
            self.resource["sync"].update(
                {
                    "status": "pending",
                    "desired_acl_revision": next_revision,
                }
            )
            return _BackendResponse(200, {"ok": True, "resource": self.resource})
        return _BackendResponse(404, {"error": "project_not_found"})

    def acknowledge_project(self) -> None:
        project = self.projection["projects"][0]
        project["sync"]["status"] = "in_sync"
        project["sync"]["applied_access_revision"] = project["sync"]["desired_access_revision"]
        self.projection["policy_sync"]["status"] = "in_sync"

    @staticmethod
    def local_client():
        return app.test_client()

    def remote_client(self, role: str = "owner"):
        client = app.test_client()
        cookie = remote_session_cookie(
            self.config,
            f"{role}@example.com",
            f"subject-{role}",
            session_claims={
                "vibe_instance_id": "inst-current",
                "vibe_instance_role": role,
                "vibe_instance_access_source": "owner" if role == "owner" else "email",
                "vibe_instance_authorization_revision": self.projection["instance"][
                    "authorization_revision"
                ],
            },
        )
        client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="permissions.avibe.bot")
        return client

    @staticmethod
    def csrf(client, origin: str = "http://127.0.0.1") -> dict[str, str]:
        return csrf_headers(client, origin)

    def close(self) -> None:
        self._backend_patch.stop()
        self._environment.stop()
        self._tempdir.cleanup()
