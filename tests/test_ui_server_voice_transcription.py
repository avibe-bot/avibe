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
import pytest

from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


@pytest.fixture(autouse=True)
def voice_allowed():
    """These cases cover transcription itself: this machine's account has said yes to voice."""
    from core.voice_consent import set_voice_consent

    set_voice_consent("local", "granted")


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
    assert response.get_json() == {
        "available": True,
        "max_file_bytes": 160_044,
        "consent": "granted",
        "app_hold": True,
        "app_hold_main_at_touch": True,
        "app_hold_start_ms": None,
        "app_hold_prepare_at_touch": True,
    }

    config.audio_asr.max_file_bytes = 160_043
    response = client.get("/api/asr/status")
    assert response.get_json() == {
        "available": False,
        "max_file_bytes": 160_043,
        "consent": "granted",
        "app_hold": True,
        "app_hold_main_at_touch": True,
        "app_hold_start_ms": None,
        "app_hold_prepare_at_touch": True,
    }

    # The off switch for the iOS App's own hold recognition: the next read says so,
    # with no restart, and voice itself stays available.
    config.audio_asr.max_file_bytes = 160_044
    config.audio_asr.app_hold = False
    response = client.get("/api/asr/status")
    assert response.get_json() == {
        "available": True,
        "max_file_bytes": 160_044,
        "consent": "granted",
        "app_hold": False,
        "app_hold_main_at_touch": True,
        "app_hold_start_ms": None,
        "app_hold_prepare_at_touch": True,
    }

    # Where the main Bot's button opens the microphone in the iOS App: the next read
    # says so too, on its own.
    config.audio_asr.app_hold = True
    config.audio_asr.app_hold_main_at_touch = False
    response = client.get("/api/asr/status")
    assert response.get_json() == {
        "available": True,
        "max_file_bytes": 160_044,
        "consent": "granted",
        "app_hold": True,
        "app_hold_main_at_touch": False,
        "app_hold_start_ms": None,
        "app_hold_prepare_at_touch": True,
    }

    # When a hold's microphone may start in the App, and whether it may prepare before:
    # said as set, and a time out of 0..500 as null (the App's own default).
    config.audio_asr.app_hold_main_at_touch = True
    config.audio_asr.app_hold_start_ms = 80
    config.audio_asr.app_hold_prepare_at_touch = False
    body = client.get("/api/asr/status").get_json()
    assert body["app_hold_start_ms"] == 80
    assert body["app_hold_prepare_at_touch"] is False
    for out_of_range in (-1, 501):
        config.audio_asr.app_hold_start_ms = out_of_range
        assert client.get("/api/asr/status").get_json()["app_hold_start_ms"] is None


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
            "reply": "先跑 make release",
            "user_previous": "部署前要做什么",
            "vocabulary": ["歪比", "产品经理"],
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
            "reply": "先跑 make release",
            "user_previous": "部署前要做什么",
            "vocabulary": ["歪比", "产品经理"],
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
            "reply": "",
            "user_previous": "",
            "vocabulary": [],
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


def _paired_asr(monkeypatch):
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


def test_voice_consent_is_asked_answered_and_enforced_per_account(monkeypatch):
    """Voice input sends speech to a third-party AI service: the status reports this
    account's answer, the answer is saved per account, and the relay transcribes only
    after a yes to the current disclosure (core/voice_consent.py)."""
    from core.chat_discovery import set_state_meta
    from core.voice_consent import VOICE_DISCLOSURE_REVISION, consent_key, set_voice_consent

    _paired_asr(monkeypatch)
    sent: list[object] = []

    async def transcribe(_self, attachment, **_kwargs):
        sent.append(attachment)
        return {"text": "hi", "cleanup": "success"}

    monkeypatch.setattr(AudioAsrService, "transcribe_voice_segment", transcribe)
    client = app.test_client()
    body = {"name": "voice.webm", "mime": "audio/webm", "data": base64.b64encode(b"audio").decode()}

    # An answer to an older disclosure, or another account's yes, does not count.
    set_state_meta(consent_key("local"), {"voice": {"choice": "granted", "revision": VOICE_DISCLOSURE_REVISION - 1}})
    set_voice_consent("remote:someone-else", "granted")
    assert client.get("/api/asr/status").get_json()["consent"] is None
    response = client.post("/api/asr/transcribe", json=body, headers=csrf_headers(client))
    assert response.status_code == 403
    assert response.get_json() == {"ok": False, "error": "voice_consent_required"}

    response = client.put("/api/asr/consent", json={"choice": "declined"}, headers=csrf_headers(client))
    assert response.get_json() == {"ok": True, "consent": "declined"}
    assert client.get("/api/asr/status").get_json()["consent"] == "declined"
    assert client.post("/api/asr/transcribe", json=body, headers=csrf_headers(client)).status_code == 403
    assert sent == []

    assert client.put("/api/asr/consent", json={"choice": "yes"}, headers=csrf_headers(client)).status_code == 400
    client.put("/api/asr/consent", json={"choice": "granted"}, headers=csrf_headers(client))
    response = client.post("/api/asr/transcribe", json=body, headers=csrf_headers(client))
    assert response.status_code == 200
    assert len(sent) == 1


def test_a_changed_answer_reaches_only_the_same_accounts_other_tabs_and_devices(monkeypatch):
    """Saving an answer announces it so the account's other open pages stop sending at once
    on a "no"; the frame reaches only that account, at the voice routes' Editor role."""
    import json

    from vibe.authorization import AuthorizationContext, can_receive_workbench_event
    from vibe.sse_broker import broker
    from vibe.ui_server import _workbench_event_visible_to_context

    published: list[tuple[str, object]] = []
    monkeypatch.setattr(broker, "publish", lambda event, data: published.append((event, data)))
    client = app.test_client()
    client.put("/api/asr/consent", json={"choice": "declined"}, headers=csrf_headers(client))
    assert published == [("voice.consent.updated", {"user_key": "local"})]

    payload = json.dumps({"type": "voice.consent.updated", "data": {"user_key": "remote:alice"}})
    alice = AuthorizationContext(instance_role="editor", is_remote=True, subject="alice")
    bob = AuthorizationContext(instance_role="editor", is_remote=True, subject="bob")
    viewer = AuthorizationContext(instance_role="viewer", is_remote=True, subject="alice")
    assert _workbench_event_visible_to_context(alice, "voice.consent.updated", payload)
    assert not _workbench_event_visible_to_context(bob, "voice.consent.updated", payload)
    assert can_receive_workbench_event(alice, "voice.consent.updated")
    assert not can_receive_workbench_event(viewer, "voice.consent.updated")
