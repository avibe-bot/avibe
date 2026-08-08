from __future__ import annotations

import base64
from types import SimpleNamespace

from config.v2_config import AudioAsrConfig, RemoteAccessConfig, VibeCloudRemoteAccessConfig
from core.audio_asr import (
    AudioAsrEmptyTranscriptError,
    AudioAsrInvalidDictationError,
    AudioAsrProtocolError,
    AudioAsrService,
    AudioAsrTimeoutError,
    AudioAsrUnavailableError,
)
from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


def test_asr_status_exposes_the_configured_file_limit(monkeypatch):
    config = SimpleNamespace(
        audio_asr=AudioAsrConfig(enabled=True, max_file_bytes=160_044),
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
    assert response.get_json() == {"available": True, "max_file_bytes": 160_044}

    config.audio_asr.max_file_bytes = 160_043
    response = client.get("/api/asr/status")
    assert response.get_json() == {"available": False, "max_file_bytes": 160_043}


def test_asr_transcribe_preserves_compatibility_timeout(monkeypatch):
    async def timeout(
        _self,
        _attachment,
        *,
        sequence,
        final,
        finalize_only,
        receipts,
        before,
        after,
        timeout_seconds,
        **_kwargs,
    ):
        assert sequence == 0
        assert final is True
        assert finalize_only is False
        assert receipts == []
        assert before == ""
        assert after == ""
        assert timeout_seconds == 155.0
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
    monkeypatch.setattr(AudioAsrService, "transcribe_voice_segment", timeout)
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
        _attachment,
        **_kwargs,
    ):
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
    monkeypatch.setattr(AudioAsrService, "transcribe_voice_segment", empty)
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
        _attachment,
        **_kwargs,
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
    monkeypatch.setattr(AudioAsrService, "transcribe_voice_segment", transcribe)
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
        _attachment,
        **_kwargs,
    ):
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
    monkeypatch.setattr(AudioAsrService, "transcribe_voice_segment", unavailable)
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


def test_asr_transcribe_forwards_segment_receipts_and_context(monkeypatch):
    calls = []

    async def transcribe(_self, attachment, **kwargs):
        assert attachment is not None
        assert attachment.name == "voice.webm"
        calls.append(kwargs)
        return {"receipt": "encrypted-receipt", "sequence": 0}

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
    monkeypatch.setattr(AudioAsrService, "transcribe_voice_segment", transcribe)
    client = app.test_client()

    response = client.post(
        "/api/asr/transcribe",
        json={
            "name": "voice.webm",
            "mime": "audio/webm",
            "data": base64.b64encode(b"audio").decode(),
            "dictation_id": "dictation-1",
            "sequence": 0,
            "overlap_ms": 250,
            "final": False,
            "receipts": [],
            "before": "",
            "after": "",
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json() == {"receipt": "encrypted-receipt", "sequence": 0}
    assert calls == [
        {
            "dictation_id": "dictation-1",
            "sequence": 0,
            "overlap_ms": 250,
            "final": False,
            "finalize_only": False,
            "receipts": [],
            "before": "",
            "after": "",
            "timeout_seconds": 155.0,
        }
    ]


def test_asr_transcribe_supports_finalize_only_without_audio(monkeypatch):
    async def transcribe(_self, attachment, **kwargs):
        assert attachment is None
        assert kwargs == {
            "dictation_id": "dictation-1",
            "sequence": 1,
            "overlap_ms": 0,
            "final": True,
            "finalize_only": True,
            "receipts": ["receipt-0"],
            "before": "前文",
            "after": "后文",
            "timeout_seconds": 155.0,
        }
        return {"text": "整理结果", "cleanup": "success"}

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
    monkeypatch.setattr(AudioAsrService, "transcribe_voice_segment", transcribe)
    client = app.test_client()

    response = client.post(
        "/api/asr/transcribe",
        json={
            "dictation_id": "dictation-1",
            "sequence": 1,
            "overlap_ms": 0,
            "final": True,
            "finalize_only": True,
            "receipts": ["receipt-0"],
            "before": "前文",
            "after": "后文",
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json() == {"text": "整理结果", "cleanup": "success"}


def test_asr_transcribe_preserves_dictation_and_protocol_errors(monkeypatch):
    config = SimpleNamespace(
        audio_asr=AudioAsrConfig(enabled=True),
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
    payload = {
        "name": "voice.webm",
        "mime": "audio/webm",
        "data": base64.b64encode(b"audio").decode(),
    }

    async def invalid(_self, _attachment, **_kwargs):
        raise AudioAsrInvalidDictationError("invalid")

    monkeypatch.setattr(AudioAsrService, "transcribe_voice_segment", invalid)
    response = client.post("/api/asr/transcribe", json=payload, headers=csrf_headers(client))
    assert response.status_code == 422
    assert response.get_json() == {"error": "invalid_dictation"}

    async def malformed(_self, _attachment, **_kwargs):
        raise AudioAsrProtocolError("malformed")

    monkeypatch.setattr(AudioAsrService, "transcribe_voice_segment", malformed)
    response = client.post("/api/asr/transcribe", json=payload, headers=csrf_headers(client))
    assert response.status_code == 502
    assert response.get_json() == {"error": "transcription_failed"}
