from __future__ import annotations

import base64
from types import SimpleNamespace

from config.v2_config import AudioAsrConfig, RemoteAccessConfig, VibeCloudRemoteAccessConfig
from core.audio_asr import AudioAsrService, AudioAsrTimeoutError
from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


def test_asr_transcribe_preserves_compatibility_timeout(monkeypatch):
    async def timeout(
        _self,
        _attachments,
        *,
        raise_on_timeout=False,
        timeout_seconds=None,
    ):
        assert raise_on_timeout is True
        assert timeout_seconds == 120.0
        raise AudioAsrTimeoutError("timed out")

    monkeypatch.setattr(
        "core.services.settings.load_config",
        lambda: SimpleNamespace(
            audio_asr=AudioAsrConfig(enabled=True),
            remote_access=RemoteAccessConfig(
                vibe_cloud=VibeCloudRemoteAccessConfig(
                    enabled=True,
                    backend_url="https://avibe.bot",
                    instance_id="instance",
                    instance_secret="secret",
                )
            ),
        ),
    )
    monkeypatch.setattr(AudioAsrService, "is_available", lambda _self: True)
    monkeypatch.setattr(AudioAsrService, "transcribe_attachments", timeout)
    client = app.test_client()

    response = client.post(
        "/api/asr/transcribe",
        json={
            "name": "voice.webm",
            "mime": "audio/webm",
            "data": base64.b64encode(b"audio").decode(),
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 504
    assert response.get_json() == {"error": "transcription_timeout"}
