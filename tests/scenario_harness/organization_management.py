from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

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
from vibe import cloud_management, remote_access
from vibe.ui_server import app


REMOTE_ORIGIN = "https://alex.avibe.bot"


class OrganizationManagementScenarioHarness:
    """Hermetic browser harness for the Cloud management authorization flow."""

    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self._environment = patch.dict(
            os.environ,
            {"AVIBE_HOME": str(Path(self._tempdir.name) / "avibe-home")},
        )
        self._environment.start()
        cloud_management.reset_for_tests()
        self.config = self._save_config()

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
        cloud.backend_url = "https://avibe.bot"
        cloud.public_url = REMOTE_ORIGIN
        cloud.client_id = "vr_client_123"
        cloud.instance_id = "inst_123"
        cloud.session_secret = "scenario-session-secret"
        cloud.issuer = "https://avibe.bot"
        cloud.jwks_uri = "https://avibe.bot/.well-known/jwks.json"
        cloud.authorization_endpoint = "https://avibe.bot/oauth/authorize"
        cloud.token_endpoint = "https://avibe.bot/oauth/token"
        cloud.redirect_uri = f"{REMOTE_ORIGIN}/auth/callback"
        config.save()
        return config

    def remote_client(self, *, subject: str = "user-1", role: str = "viewer"):
        client = app.test_client()
        cookie = remote_session_cookie(
            self.config,
            "alex@example.com",
            subject,
            role=role,
            access_source="email",
        )
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            cookie,
            domain="alex.avibe.bot",
        )
        return client

    def unbound_remote_client(self):
        return app.test_client()

    @staticmethod
    def csrf(client, origin: str = REMOTE_ORIGIN) -> dict[str, str]:
        return csrf_headers(client, origin)

    def close(self) -> None:
        cloud_management.reset_for_tests()
        self._environment.stop()
        self._tempdir.cleanup()
