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
                "path": "cloud",
                "providerStage": "response",
                "sizeBytes": 240_000,
                "mimeType": "audio/webm",
                "durationMs": 60_000,
                "elapsedMs": 820,
                "httpStatus": 200,
                "attemptCount": 1,
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
    assert metric["sizeBytes"] == 240_000
    assert metric["httpStatus"] == 200
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


def test_voice_telemetry_rejects_non_object_payload():
    client = app.test_client()
    response = client.post(
        "/api/asr/telemetry",
        json=["segment_transcription"],
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_payload"}
