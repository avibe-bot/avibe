"""``vibe model-requests``: list, show, and export the model requests Vibey kept (``request_artifacts``).

The JSON on stdout names each request by its typed facts and never carries a request or response body; export
copies one into a new private directory under the directory the caller names.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timezone

import pytest

from config import paths
from core.agent_core.ai.request_artifacts import RESPONSE_FILE, RequestArtifactStore, ResponseCapture
from vibe import cli

BODY = b'{"messages":[{"role":"user","content":"private prompt text"}]}'
ERROR = b'{"error":{"message":"refused: private prompt text"}}'


def _run(monkeypatch, capsys, *argv: str) -> tuple[int, dict]:
    monkeypatch.setattr(sys, "argv", ["vibe", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    captured = capsys.readouterr()
    assert b"private prompt" not in (captured.out + captured.err).encode()
    return exc.value.code, json.loads(captured.out or captured.err)


@pytest.fixture
def kept() -> str:
    store = RequestArtifactStore(paths.get_model_request_artifacts_dir())
    meta = {
        "state": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "owner": {"pid": os.getpid(), "started": None},
        "session_id": "ses_one",
        "turn_id": "trn_one",
        "request": {"protocol": "anthropic", "provider": "anthropic", "model": "claude-opus-5-5", "body_bytes": len(BODY)},
    }
    artifact_id = store.begin(meta, BODY)
    capture = ResponseCapture()
    capture.response(400, {"x-request-id": "relay-1", "authorization": "Bearer secret"})
    capture.feed(ERROR)
    capture.finish()
    store.fail(artifact_id, {"state": "failed", "outcome": {"kind": "http_error", "error_kind": "invalid_request",
                                                             "http_status": 400}}, capture)
    return artifact_id


def test_list_and_show_name_a_kept_request_without_its_bodies(monkeypatch, capsys, kept):
    code, listed = _run(monkeypatch, capsys, "model-requests", "list")

    assert code == 0 and listed["pagination"]["has_more"] is False
    (item,) = listed["model_requests"]
    assert item["id"] == kept and item["state"] == "failed" and item["session_id"] == "ses_one"
    assert item["outcome"] == "http_error" and item["http_status"] == 400 and item["request_bytes"] == len(BODY)

    code, other = _run(monkeypatch, capsys, "model-requests", "list", "--session-id", "ses_other")
    assert code == 0 and other["model_requests"] == []

    code, shown = _run(monkeypatch, capsys, "model-requests", "show", kept)
    assert code == 0
    assert shown["model_request"]["response"]["headers"] == {"x-request-id": "relay-1"}
    assert shown["files"]["request.bin"] == len(BODY) and shown["files"][RESPONSE_FILE] == len(ERROR)


def test_export_copies_into_a_new_private_directory_and_refuses_to_overwrite(monkeypatch, capsys, kept, tmp_path):
    destination = tmp_path / "exported"

    code, exported = _run(monkeypatch, capsys, "model-requests", "export", kept, "--to", str(destination))

    assert code == 0 and exported["path"] == str(destination / kept)
    assert exported["files"] == ["meta.json", "request.bin", "response.bin"]
    assert (destination / kept / "request.bin").read_bytes() == BODY
    assert stat.S_IMODE(os.stat(destination / kept).st_mode) == 0o700

    code, error = _run(monkeypatch, capsys, "model-requests", "export", kept, "--to", str(destination))
    assert code == 1 and error["code"] == "model_request_export_exists"


@pytest.mark.parametrize("artifact_id", ["../../etc", "mr_20261031T000000Z_000000000000"])
def test_an_unknown_or_invalid_id_reads_nothing(monkeypatch, capsys, artifact_id):
    code, error = _run(monkeypatch, capsys, "model-requests", "show", artifact_id)

    assert code == 1 and error["code"] == "model_request_unavailable"
