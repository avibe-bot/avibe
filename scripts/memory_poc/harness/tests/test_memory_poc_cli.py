from __future__ import annotations

from memory_poc.cli import main


def test_report_rejects_path_traversal_run_id(capsys) -> None:
    assert main(["report", "--run-id", "../outside"]) == 2
    assert "invalid_run_id" in capsys.readouterr().err
