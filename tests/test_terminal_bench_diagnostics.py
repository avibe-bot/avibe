"""Response diagnosis retains stop/shape/usage, never private model content."""
import json
import sqlite3
from unittest.mock import Mock

import pytest

from scripts.terminal_bench.diagnostics import project_responses, response_query
from scripts.terminal_bench.profile import Profile
from scripts.terminal_bench.runner import FullRuntimeTrial


@pytest.mark.parametrize("stop,shape", [("stop", "empty"), ("stop", "thinking"), ("length", "thinking"), ("length", "text"), ("tool_use", "tool_call")])
def test_query_preserves_cause_without_exporting_model_text(stop, shape):
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE messages(session_id,type,context_seq,content_json)")
        blocks = [] if shape == "empty" else [{"type": shape, "text": "SECRET-隐私", "arguments": {"key": "SECRET-隐私"}}]
        message = {"role": "assistant", "content": blocks, "stop_reason": stop, "error_message": "SECRET-隐私", "usage": {"output_tokens": 32000}}
        for sid in ("ses_test", "ses_unrelated"):
            db.execute("INSERT INTO messages VALUES(?,?,?,?)", (sid, "error", 2, json.dumps({"model": {"message": message}})))
        rows = [dict(r) for r in db.execute(response_query("ses_test"))]
    assert len(rows) == 1
    assert "SECRET" not in json.dumps(rows)
    report = project_responses({"ok": True, "rows": rows})
    last = report["responses"][0]
    assert last["stop_reason"] == stop
    assert last["thinking_blocks"] == int(shape == "thinking")
    assert last["text_blocks"] == int(shape == "text")
    assert last["tool_calls"] == int(shape == "tool_call")
    assert last["output_tokens"] == 32000
    assert report["raw_text_exported"] is False


def test_projection_is_bounded_and_does_not_trust_cli_extra_fields():
    row = {"stop_reason": "SECRET", "row_type": "SECRET", "text_chars": "SECRET", "context_seq": 3,
           "input_tokens": -3, "output_tokens": True, "content_text": "SECRET"}
    report = project_responses({"ok": True, "rows": [row] * 100})
    assert len(report["responses"]) == 8
    assert "SECRET" not in json.dumps(report)
    assert report["responses"][0]["input_tokens"] is None
    assert report["responses"][0]["output_tokens"] is None
    assert "LIMIT 32" in response_query("ses_test")
    for sid in ("x' OR 1=1 --", "", "x" * 129):
        with pytest.raises(ValueError):
            response_query(sid)
    with pytest.raises(ValueError):
        project_responses({"ok": False, "error": "SECRET"})


def test_missing_diagnostics_does_not_mask_run_failure_or_skip_cleanup(tmp_path, monkeypatch):
    task = tmp_path / "task"
    task.mkdir()
    trial = FullRuntimeTrial(task, tmp_path / "state", tmp_path / "out", Profile(model="fixture", base_url="http://127.0.0.1:9/v1"))
    trial._result.update(outcome="failed", session_id="ses_test")
    trial._service = Mock()
    trial._service.poll.return_value = None
    trial._processes = Mock()
    trial._processes.close.return_value = {"owned_processes_stopped": True}
    monkeypatch.setattr(trial, "_call", Mock(side_effect=ValueError("SECRET")))
    report = trial.close()
    assert report["outcome"] == "failed"
    assert report["diagnostics"]["status"] == "unavailable"
    trial._processes.close.assert_called_once()
    assert "SECRET" not in json.dumps(report)
