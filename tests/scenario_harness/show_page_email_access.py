from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

from config.v2_config import (
    AgentsConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from core.show_pages import ShowPageStore
from vibe import remote_access, ui_server
from vibe.ui_server import app


REMOTE_ORIGIN = "https://alex.avibe.bot"
REMOTE_PEER = {"REMOTE_ADDR": "203.0.113.10"}


class ShowPageEmailAccessScenarioHarness:
    """Hermetic browser harness for one exact-email Show Page login."""

    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self._environment = patch.dict(
            os.environ,
            {"AVIBE_HOME": str(Path(self._tempdir.name) / "avibe-home")},
        )
        self._environment.start()
        remote_access._oauth_handshakes.clear()
        self.config = self._save_config()
        self.client = app.test_client()
        store = ShowPageStore()
        try:
            store.ensure("session-one")
            store.ensure("session-two")
        finally:
            store.close()

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
        cloud.public_url = REMOTE_ORIGIN
        cloud.client_id = "vr_client_123"
        cloud.instance_id = "inst_123"
        cloud.session_secret = "scenario-session-secret"
        cloud.authorization_endpoint = "https://avibe.bot/oauth/authorize"
        cloud.redirect_uri = f"{REMOTE_ORIGIN}/auth/callback"
        config.save()
        return config

    def begin_login(self, show_page_id: str) -> dict[str, str]:
        next_path = f"/show/{show_page_id}/__show/me"
        response = self.client.get(
            next_path,
            base_url=REMOTE_ORIGIN,
            environ_base=REMOTE_PEER,
            follow_redirects=False,
        )
        assert response.status_code == 302
        authorize_params = httpx.URL(response.headers["Location"]).params
        state = authorize_params["state"]
        state_payload = ui_server._read_oauth_state(
            self.config.remote_access.vibe_cloud.session_secret,
            state,
        )
        assert state_payload is not None
        handshake = remote_access._oauth_handshakes[state_payload["r"]]
        return {
            "next_path": next_path,
            "show_page_id": authorize_params["show_page_id"],
            "state": state,
            "nonce": handshake["nonce"],
        }

    def complete_login(self, handshake: dict[str, str]):
        show_page_id = handshake["show_page_id"]
        exchange_result = {
            "claims": {
                "email": "guest@example.com",
                "sub": "guest-1",
                "nonce": handshake["nonce"],
            },
            "session_claims": {
                "vibe_instance_id": "inst_123",
                "vibe_instance_role": "viewer",
                "vibe_instance_access_source": "show_page_email",
                "vibe_show_page_id": show_page_id,
            },
        }
        with patch.object(remote_access, "exchange_oauth_code", return_value=exchange_result):
            return self.client.get(
                f"/auth/callback?code=scenario-code&state={handshake['state']}",
                base_url=REMOTE_ORIGIN,
                environ_base=REMOTE_PEER,
                follow_redirects=False,
            )

    def get(self, path: str):
        return self.client.get(
            path,
            base_url=REMOTE_ORIGIN,
            environ_base=REMOTE_PEER,
            follow_redirects=False,
        )

    def close(self) -> None:
        remote_access._oauth_handshakes.clear()
        self._environment.stop()
        self._tempdir.cleanup()
