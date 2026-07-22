from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_poc.cli import main
from memory_poc.environment import ProviderSettings
from memory_poc.errors import HarnessError
from memory_poc.paths import write_private_text
from memory_poc.reports import build_report


def test_report_rejects_path_traversal_run_id(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr("memory_poc.cli.verify_harness_interpreter", lambda _workspace: Path(sys.executable))

    assert main(["report", "--run-id", "../outside"]) == 2
    assert "invalid_run_id" in capsys.readouterr().err


def test_cli_requires_the_locked_harness_interpreter(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def reject_host_python(_workspace: Path) -> Path:
        raise HarnessError("harness_interpreter_not_locked")

    monkeypatch.setattr("memory_poc.cli.verify_harness_interpreter", reject_host_python)

    assert main(["report", "--run-id", "r1"]) == 2
    assert "harness_interpreter_not_locked" in capsys.readouterr().err


def test_report_command_rejects_fixture_text_in_a_persisted_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / ".runtime" / "memory-poc"
    run_dir = state / "runs" / "r1"
    run_dir.mkdir(parents=True)
    settings = ProviderSettings(
        llm_base_url="https://example.invalid/v1",
        llm_model="llm-model",
        llm_api_key="not-a-real-key",
        embedding_base_url="https://example.invalid/v1",
        embedding_model="embedding-model",
        embedding_api_key="also-not-a-real-key",
        source=tmp_path / ".env.poc",
    )
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=settings)
    report["duplicates"]["observed"] = "fixture body"
    write_private_text(
        run_dir / "run.json",
        json.dumps({"stage": "sanity", "fixture_set": "stage1-mini"}),
        anchor=state,
    )
    write_private_text(run_dir / "report.json", json.dumps(report), anchor=state)
    monkeypatch.setattr("memory_poc.cli.checked_workspace_root", lambda: workspace)
    monkeypatch.setattr("memory_poc.cli.verify_harness_interpreter", lambda _workspace: Path(sys.executable))
    monkeypatch.setattr("memory_poc.cli.runtime_root", lambda _workspace: state)
    monkeypatch.setattr(
        "memory_poc.cli.load_sanity_fixture",
        lambda: SimpleNamespace(messages=[{"content": "fixture body"}]),
    )

    assert main(["report", "--run-id", "r1"]) == 2
    output = capsys.readouterr().err
    assert "report_contains_fixture_text" in output
    assert "fixture body" not in output


def test_report_command_rejects_a_persisted_secret_substring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / ".runtime" / "memory-poc"
    run_dir = state / "runs" / "r1"
    run_dir.mkdir(parents=True)
    settings = ProviderSettings(
        llm_base_url="https://example.invalid/v1",
        llm_model="llm-model",
        llm_api_key="test-api-key-0123456789",
        embedding_base_url="https://example.invalid/v1",
        embedding_model="embedding-model",
        embedding_api_key="also-not-a-real-key",
        source=tmp_path / ".env.poc",
    )
    monkeypatch.setattr("memory_poc.reports.lock_id", lambda: "lock")
    report = build_report(run_id="r1", settings=settings)
    report["environment"]["llm_model"] = f"alias-{settings.llm_api_key}"
    write_private_text(
        run_dir / "run.json",
        json.dumps({"stage": "sanity", "fixture_set": "stage1-mini"}),
        anchor=state,
    )
    write_private_text(run_dir / "report.json", json.dumps(report), anchor=state)
    monkeypatch.setattr("memory_poc.cli.checked_workspace_root", lambda: workspace)
    monkeypatch.setattr("memory_poc.cli.verify_harness_interpreter", lambda _workspace: Path(sys.executable))
    monkeypatch.setattr("memory_poc.cli.runtime_root", lambda _workspace: state)
    monkeypatch.setattr("memory_poc.cli.discover_provider_settings", lambda _workspace: settings)
    monkeypatch.setattr(
        "memory_poc.cli.load_sanity_fixture",
        lambda: SimpleNamespace(messages=[{"content": "fixture body"}]),
    )

    assert main(["report", "--run-id", "r1"]) == 2
    captured = capsys.readouterr()
    assert "report_contains_secret" in captured.err
    assert settings.llm_api_key not in captured.out
    assert settings.llm_api_key not in captured.err


def test_report_command_fails_closed_for_an_unknown_fixture_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / ".runtime" / "memory-poc"
    run_dir = state / "runs" / "r1"
    run_dir.mkdir(parents=True)
    write_private_text(
        run_dir / "run.json",
        json.dumps({"stage": "quality", "fixture_set": "future-corpus"}),
        anchor=state,
    )
    write_private_text(run_dir / "report.json", '{"fixture":"future fixture body"}', anchor=state)
    monkeypatch.setattr("memory_poc.cli.checked_workspace_root", lambda: workspace)
    monkeypatch.setattr("memory_poc.cli.verify_harness_interpreter", lambda _workspace: Path(sys.executable))
    monkeypatch.setattr("memory_poc.cli.runtime_root", lambda _workspace: state)
    monkeypatch.setattr(
        "memory_poc.cli.load_sanity_fixture",
        lambda: pytest.fail("unknown report source must not load a sanity fixture"),
    )

    assert main(["report", "--run-id", "r1"]) == 2
    output = capsys.readouterr().err
    assert "report_fixture_source_unknown" in output
    assert "future fixture body" not in output
