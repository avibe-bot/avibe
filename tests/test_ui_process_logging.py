from __future__ import annotations

import io
import json
import logging
import re

import pytest

from config import paths
from tests.ui_server_test_helpers import csrf_headers
from vibe import service_logging, ui_server
from vibe.ui_server import app


@pytest.fixture
def ui_logging(monkeypatch, tmp_path):
    service_logging.shutdown_service_logging()
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    try:
        yield tmp_path
    finally:
        service_logging.shutdown_service_logging()
        assert root.handlers == previous_handlers
        assert root.level == previous_level


def _flush_ui_logging() -> None:
    runtime = service_logging._runtime
    assert runtime is not None
    runtime.handler.queue.join()


def test_ui_process_log_line_has_timestamp_and_stderr_output(ui_logging, capsys):
    ui_server._configure_ui_logging()
    logging.getLogger("ui-process-test").warning("timestamped UI warning")
    _flush_ui_logging()
    service_logging.shutdown_service_logging()

    log_text = (ui_logging / "logs" / "vibe_ui.log").read_text(encoding="utf-8")
    pattern = re.compile(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - ui-process-test - WARNING - \["
    )
    assert pattern.search(log_text)
    assert pattern.search(capsys.readouterr().err)


def test_voice_reliability_is_persisted_and_redacted(ui_logging, monkeypatch):
    stderr = io.StringIO()
    monkeypatch.setattr(ui_server.sys, "stderr", stderr)
    ui_server._configure_ui_logging()
    client = app.test_client()
    response = client.post(
        "/api/asr/telemetry",
        json={
            "event": "segment_transcription",
            "outcome": "success",
            "dictationId": "dictation-123",
            "sizeBytes": 240_000,
            "httpStatus": 200,
            "transcript": "private dictation text",
            "reply": "private reply context",
            "user_previous": "private previous context",
            "vocabulary": "private vocabulary",
            "tokens": "private tokens",
            "audio": "private audio bytes",
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    _flush_ui_logging()
    service_logging.shutdown_service_logging()

    log_text = (ui_logging / "logs" / "vibe_ui.log").read_text(encoding="utf-8")
    line = next(line for line in log_text.splitlines() if "voice_reliability " in line)
    metric = json.loads(line.split("voice_reliability ", 1)[1])
    assert metric["event"] == "segment_transcription"
    assert metric["dictationId"] == "dictation-123"
    assert metric["sizeBytes"] == 240_000
    assert metric["httpStatus"] == 200
    for forbidden_field in (
        '"transcript":',
        '"reply":',
        '"user_previous":',
        '"vocabulary":',
        '"tokens":',
        '"audio":',
    ):
        assert forbidden_field not in log_text
    for forbidden_value in (
        "private dictation text",
        "private reply context",
        "private previous context",
        "private vocabulary",
        "private tokens",
        "private audio bytes",
    ):
        assert forbidden_value not in log_text
    assert " - voice_reliability " not in stderr.getvalue()


def test_ui_log_uses_bounded_rotating_file(ui_logging, monkeypatch):
    monkeypatch.setattr(ui_server.sys, "stderr", io.StringIO())
    monkeypatch.setattr(ui_server, "APPLICATION_LOG_MAX_BYTES", 256)
    monkeypatch.setattr(ui_server, "APPLICATION_LOG_BACKUP_COUNT", 2)
    ui_server._configure_ui_logging()
    for index in range(10):
        logging.getLogger("ui-rotation-test").info("rotation-record-%s %s", index, "x" * 80)
    service_logging.shutdown_service_logging()

    files = sorted((ui_logging / "logs").glob("vibe_ui.log*"))
    assert [path.name for path in files] == ["vibe_ui.log", "vibe_ui.log.1", "vibe_ui.log.2"]
    assert "rotation-record-9" in files[0].read_text(encoding="utf-8")
    assert "rotation-record-8" in files[1].read_text(encoding="utf-8")
    assert "rotation-record-7" in files[2].read_text(encoding="utf-8")
