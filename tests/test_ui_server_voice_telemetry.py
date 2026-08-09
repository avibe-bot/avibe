from __future__ import annotations

import json
import logging

from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


def test_voice_telemetry_logs_only_allowlisted_metadata(caplog):
    client = app.test_client()
    with caplog.at_level(logging.INFO, logger="vibe.ui_server"):
        response = client.post(
            "/api/asr/telemetry",
            json={
                "event": "segment_transcription",
                "outcome": "success",
                "dictationId": "dictation-123",
                "path": "cloud",
                "providerStage": "response",
                "sizeBytes": 240_000,
                "mimeType": "audio/webm",
                "durationMs": 60_000,
                "elapsedMs": 820,
                "httpStatus": 200,
                "attemptCount": 1,
                "realtime": True,
                "firstPreviewMs": 360,
                "stopToFinalMs": 420,
                "browserFamily": "chrome",
                "transcript": "private words",
                "audio": "private bytes",
                "token": "secret credential",
            },
            headers=csrf_headers(client),
        )

    assert response.status_code == 200
    record = next(record for record in caplog.records if record.message.startswith("voice_reliability "))
    metric = json.loads(record.message.removeprefix("voice_reliability "))
    assert metric["event"] == "segment_transcription"
    assert metric["dictationId"] == "dictation-123"
    assert metric["sizeBytes"] == 240_000
    assert metric["httpStatus"] == 200
    assert metric["realtime"] is True
    assert metric["firstPreviewMs"] == 360
    assert metric["stopToFinalMs"] == 420
    assert metric["release"]
    assert "transcript" not in metric
    assert "audio" not in metric
    assert "token" not in metric
    assert "private" not in record.message
    assert "secret" not in record.message


def test_voice_telemetry_rejects_unknown_events():
    client = app.test_client()
    response = client.post(
        "/api/asr/telemetry",
        json={"event": "audio_contents", "outcome": "success"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_event"}


def test_voice_telemetry_accepts_insertion_timing(caplog):
    client = app.test_client()
    with caplog.at_level(logging.INFO, logger="vibe.ui_server"):
        response = client.post(
            "/api/asr/telemetry",
            json={
                "event": "dictation_inserted",
                "outcome": "success",
                "providerStage": "finalization",
                "attemptCount": 1,
                "stopToInsertionMs": 820,
            },
            headers=csrf_headers(client),
        )

    assert response.status_code == 200
    record = next(record for record in caplog.records if record.message.startswith("voice_reliability "))
    metric = json.loads(record.message.removeprefix("voice_reliability "))
    assert metric["event"] == "dictation_inserted"
    assert metric["stopToInsertionMs"] == 820


def test_voice_telemetry_rejects_unknown_outcomes():
    client = app.test_client()
    for outcome in ("sort-of-worked", ["success"]):
        response = client.post(
            "/api/asr/telemetry",
            json={"event": "segment_transcription", "outcome": outcome},
            headers=csrf_headers(client),
        )

        assert response.status_code == 400
        assert response.get_json() == {"error": "invalid_outcome"}


def test_voice_telemetry_rejects_fractional_counts():
    client = app.test_client()
    response = client.post(
        "/api/asr/telemetry",
        json={
            "event": "dictation_finalized",
            "outcome": "success",
            "segmentCount": 1.5,
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_field", "field": "segmentCount"}


def test_voice_telemetry_rejects_content_disguised_as_mime_type(caplog):
    client = app.test_client()
    with caplog.at_level(logging.INFO, logger="vibe.ui_server"):
        response = client.post(
            "/api/asr/telemetry",
            json={
                "event": "segment_transcription",
                "outcome": "failed",
                "mimeType": "private transcript words",
            },
            headers=csrf_headers(client),
        )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_field", "field": "mimeType"}
    assert not any(record.message.startswith("voice_reliability ") for record in caplog.records)


def test_voice_telemetry_rejects_content_disguised_as_dictation_id(caplog):
    client = app.test_client()
    with caplog.at_level(logging.INFO, logger="vibe.ui_server"):
        response = client.post(
            "/api/asr/telemetry",
            json={
                "event": "dictation_finalized",
                "outcome": "failed",
                "dictationId": "private transcript words",
            },
            headers=csrf_headers(client),
        )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_field", "field": "dictationId"}
    assert not any(record.message.startswith("voice_reliability ") for record in caplog.records)


def test_voice_telemetry_rejects_non_object_payload():
    client = app.test_client()
    response = client.post(
        "/api/asr/telemetry",
        json=["segment_transcription"],
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_payload"}
