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


def test_voice_telemetry_records_a_silence_gate_without_content(caplog):
    client = app.test_client()
    with caplog.at_level(logging.INFO, logger="vibe.ui_server"):
        response = client.post(
            "/api/asr/telemetry",
            json={
                "event": "dictation_gated",
                "outcome": "empty",
                "providerStage": "finalization",
                "totalDurationMs": 520,
                "voicedReadings": 1,
                "peakDbfs": -36,
                "transcript": "Vibe, Vibey, Avibe",
            },
            headers=csrf_headers(client),
        )

    assert response.status_code == 200
    record = next(record for record in caplog.records if record.message.startswith("voice_reliability "))
    metric = json.loads(record.message.removeprefix("voice_reliability "))
    assert metric["event"] == "dictation_gated"
    assert metric["totalDurationMs"] == 520
    assert metric["voicedReadings"] == 1
    assert metric["peakDbfs"] == -36
    assert "transcript" not in metric


def test_voice_telemetry_rejects_a_peak_above_full_scale():
    client = app.test_client()
    for peak in (3, -201, -36.5, True):
        response = client.post(
            "/api/asr/telemetry",
            json={"event": "dictation_gated", "outcome": "empty", "peakDbfs": peak},
            headers=csrf_headers(client),
        )

        assert response.status_code == 400
        assert response.get_json() == {"error": "invalid_field", "field": "peakDbfs"}



def _capture_trace(**overrides):
    trace = {
        "event": "capture_trace",
        "outcome": "empty",
        "dictationId": "dictation-1",
        "entry": "hold",
        "surface": "standalone",
        "captureClass": "no_frames",
        "stop": "error",
        "mic": "ok",
        "micMs": 180,
        "totalMs": 3137,
        "pageAgeMs": 600_000,
        "hiddenCount": 2,
        "sinceHiddenMs": 90_000,
        "prewarm": "reused",
        "prewarmAgeMs": 40_000,
        "audioSession": "auto",
        "audioSessionState": "active",
        "audioSessionStateEnd": "active",
        "recorderTypes": ["audio/mp4", "audio/webm"],
        "chunks": 0,
        "levels": 0,
        "trackEvents": [["mute", 1200], ["unmute", 1500]],
        "attempts": [
            {
                "kind": "initial",
                "atMs": 190,
                "contextState": "running",
                "resumedState": "running",
                "resumeMs": 0,
                "sampleRate": 48_000,
                "module": "prewarmed",
                "chunks": 0,
                "levels": 0,
                "clockAdvanceMs": 0,
                "renderCalls": 376,
                "emptyInputs": 376,
                "trackStart": "live",
                "trackEnd": "live",
                "end": "no_frames",
            },
            {
                "kind": "microphone",
                "atMs": 2100,
                "mic": "ok",
                "micMs": 90,
                "contextState": "suspended",
                "resumedState": "running",
                "module": "ok",
                "moduleMs": 12,
                "chunks": 0,
                "levels": 0,
                "trackStart": "live",
                "end": "no_frames",
            },
        ],
        "browserFamily": "safari",
    }
    trace.update(overrides)
    return trace


def _logged_metric(caplog):
    record = next(record for record in caplog.records if record.message.startswith("voice_reliability "))
    return record, json.loads(record.message.removeprefix("voice_reliability "))


def test_voice_telemetry_logs_a_capture_trace_and_only_its_known_states(caplog):
    client = app.test_client()
    trace = _capture_trace()
    # What a device names, and what a browser says, are never kept, top level or per attempt.
    trace["deviceLabel"] = "iPhone Microphone"
    trace["attempts"][0]["deviceId"] = "device-id-secret"
    trace["attempts"][0]["label"] = "iPhone Microphone"
    with caplog.at_level(logging.INFO, logger="vibe.ui_server"):
        response = client.post("/api/asr/telemetry", json=trace, headers=csrf_headers(client))

    assert response.status_code == 200
    record, metric = _logged_metric(caplog)
    expected = _capture_trace()
    expected.pop("browserFamily")
    for key, value in expected.items():
        assert metric[key] == value, key
    assert "deviceLabel" not in metric
    assert "iPhone" not in record.message
    assert "device-id-secret" not in record.message


def test_voice_telemetry_rejects_a_malformed_capture_trace():
    client = app.test_client()
    attempt_with_text = _capture_trace()
    attempt_with_text["attempts"][0]["end"] = "the graph said hello"
    too_many_attempts = _capture_trace(attempts=[{"kind": "context"}] * 7)
    for payload, field in (
        (attempt_with_text, "attempts"),
        (too_many_attempts, "attempts"),
        (_capture_trace(attempts=["initial"]), "attempts"),
        (_capture_trace(trackEvents=[["spoke", 10]]), "trackEvents"),
        (_capture_trace(trackEvents=[["mute", -1]]), "trackEvents"),
        (_capture_trace(surface="kiosk"), "surface"),
        (_capture_trace(recorderTypes=["video/x-custom; codec=secret"]), "recorderTypes"),
        (_capture_trace(mic="Permission denied by the user"), "mic"),
        (_capture_trace(hiddenCount=1.5), "hiddenCount"),
    ):
        response = client.post("/api/asr/telemetry", json=payload, headers=csrf_headers(client))
        assert response.status_code == 400, field
        assert response.get_json() == {"error": "invalid_field", "field": field}


def test_voice_telemetry_logs_how_a_hold_ended(caplog):
    client = app.test_client()
    with caplog.at_level(logging.INFO, logger="vibe.ui_server"):
        for outcome in ("sent", "too_short", "no_frames", "muted", "denied", "asking", "kept", "interrupted", "cancelled"):
            response = client.post(
                "/api/asr/telemetry",
                json={
                    "event": "dictation_finalized",
                    "outcome": outcome,
                    "entry": "hold",
                    "captureClass": "no_frames",
                    "interruptReason": "failed",
                    "dictationId": "take-1",
                },
                headers=csrf_headers(client),
            )
            assert response.status_code == 200, outcome

    metrics = [
        json.loads(record.message.removeprefix("voice_reliability "))
        for record in caplog.records
        if record.message.startswith("voice_reliability ")
    ]
    assert [metric["outcome"] for metric in metrics] == [
        "sent", "too_short", "no_frames", "muted", "denied", "asking", "kept", "interrupted", "cancelled",
    ]
    assert all(metric["entry"] == "hold" and metric["captureClass"] == "no_frames" for metric in metrics)
