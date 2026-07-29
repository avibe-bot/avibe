from __future__ import annotations

import base64
from types import SimpleNamespace

from config.v2_config import AudioAsrConfig, RemoteAccessConfig, VibeCloudRemoteAccessConfig
from core.audio_asr import (
    AudioAsrEmptyTranscriptError,
    AudioAsrService,
    AudioAsrTimeoutError,
    AudioAsrUnavailableError,
)
from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


def test_asr_status_exposes_the_configured_file_limit(monkeypatch):
    config = SimpleNamespace(
        audio_asr=AudioAsrConfig(enabled=True, max_file_bytes=50),
        remote_access=RemoteAccessConfig(
            vibe_cloud=VibeCloudRemoteAccessConfig(
                enabled=True,
                backend_url="https://avibe.bot",
                instance_id="instance",
                instance_secret="secret",
            )
        ),
    )
    monkeypatch.setattr("core.services.settings.load_config", lambda: config)
    monkeypatch.setattr(AudioAsrService, "is_available", lambda _self: True)
    client = app.test_client()

    response = client.get("/api/asr/status")

    assert response.status_code == 200
    assert response.get_json() == {"available": True, "max_file_bytes": 50}

    config.audio_asr.max_file_bytes = 45
    response = client.get("/api/asr/status")
    assert response.get_json() == {"available": False, "max_file_bytes": 45}


def test_asr_transcribe_preserves_compatibility_timeout(monkeypatch):
    async def timeout(
        _self,
        _attachments,
        *,
        raise_on_empty=False,
        raise_on_timeout=False,
        raise_on_unavailable=False,
        timeout_seconds=None,
    ):
        assert raise_on_empty is True
        assert raise_on_timeout is True
        assert raise_on_unavailable is True
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


def test_asr_transcribe_preserves_empty_transcript_classification(monkeypatch):
    async def empty(
        _self,
        _attachments,
        *,
        raise_on_empty=False,
        raise_on_timeout=False,
        raise_on_unavailable=False,
        timeout_seconds=None,
    ):
        assert raise_on_empty is True
        assert raise_on_timeout is True
        assert raise_on_unavailable is True
        assert timeout_seconds == 120.0
        raise AudioAsrEmptyTranscriptError("empty")

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
    monkeypatch.setattr(AudioAsrService, "transcribe_attachments", empty)
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

    assert response.status_code == 422
    assert response.get_json() == {"error": "transcription_empty"}


def test_asr_transcribe_preserves_configured_size_rejection(monkeypatch):
    transcribe_called = False

    async def transcribe(
        _self,
        _attachments,
        *,
        raise_on_empty=False,
        raise_on_timeout=False,
        raise_on_unavailable=False,
        timeout_seconds=None,
    ):
        nonlocal transcribe_called
        transcribe_called = True
        return []

    monkeypatch.setattr(
        "core.services.settings.load_config",
        lambda: SimpleNamespace(
            audio_asr=AudioAsrConfig(enabled=True, max_file_bytes=4),
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
    monkeypatch.setattr(AudioAsrService, "transcribe_attachments", transcribe)
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

    assert response.status_code == 413
    assert response.get_json() == {"error": "file_too_large"}
    assert transcribe_called is False


def test_asr_transcribe_preserves_provider_unavailable_classification(monkeypatch):
    async def unavailable(
        _self,
        _attachments,
        *,
        raise_on_empty=False,
        raise_on_timeout=False,
        raise_on_unavailable=False,
        timeout_seconds=None,
    ):
        assert raise_on_empty is True
        assert raise_on_timeout is True
        assert raise_on_unavailable is True
        assert timeout_seconds == 120.0
        raise AudioAsrUnavailableError("unavailable")

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
    monkeypatch.setattr(AudioAsrService, "transcribe_attachments", unavailable)
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

    assert response.status_code == 503
    assert response.get_json() == {"error": "asr_unavailable"}
