"""``vibe bot`` and ``vibe agent run --bot`` (``docs/plans/bots.md`` D4)."""

from __future__ import annotations

import json
import re
import shlex
import sys
from unittest.mock import patch

import pytest

import vibe.cli as cli
from storage.importer import ensure_sqlite_state


@pytest.fixture()
def home(monkeypatch, tmp_path):
    for key in ("AVIBE_SESSION_ID", "AVIBE_RUN_ID", "AVIBE_CALLER_SOURCE", "AVIBE_CALLER_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = cli.VibeAgentStore()
    try:
        store.ensure_builtin_default_agents([])
    finally:
        store.close()
    return tmp_path


def _run(monkeypatch, capsys, *argv: str) -> tuple[int, dict]:
    monkeypatch.setattr(sys, "argv", ["vibe", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    captured = capsys.readouterr()
    return exc.value.code, json.loads(captured.out or captured.err)


def test_bot_commands_print_json_envelopes(home, monkeypatch, capsys):
    code, created = _run(
        monkeypatch, capsys, "bot", "create", "--name", "首席安全官", "--handle", "cso", "--role", "Reviews security."
    )
    assert code == 0, created
    assert (created["kind"], created["bot"]["handle"], created["bot"]["directory"]) == (
        "bot", "cso", str(home / "bots" / "cso"),
    )
    code, listed = _run(monkeypatch, capsys, "bot", "list")
    assert [bot["handle"] for bot in listed["bots"]] == ["main", "cso"]
    code, updated = _run(monkeypatch, capsys, "bot", "update", "cso", "--emoji", "🛡️", "--hue", "210")
    assert updated["bot"]["avatar"] == {"kind": "emoji", "emoji": "🛡️", "hue": 210}
    code, shown = _run(monkeypatch, capsys, "bot", "show", "main")
    assert (code, shown["bot"]["session_id"]) == (0, "ses_main")
    code, removed = _run(monkeypatch, capsys, "bot", "remove", "cso")
    assert (code, removed["kind"], removed["session_id"]) == (0, "bot_removed", "ses_cso")
    code, refused = _run(monkeypatch, capsys, "bot", "remove", "main")
    assert (code, refused["code"]) == (1, "bot_main_not_removable")
    code, again = _run(monkeypatch, capsys, "bot", "set", "cso", "--name", "CSO")
    assert (code, again["bot"]["session_id"]) == (0, "ses_cso")


def test_bot_commands_are_the_owners():
    for leaf in ("list", "show", "create", "set", "update", "remove"):
        assert cli._CLI_COMMAND_FLOORS[("bot", leaf)] == "owner"


def _agent_run(home, capsys, argv):
    request_store = cli.TaskExecutionStore(home / "task_requests")
    args = cli.build_parser().parse_args(["agent", "run", *argv])
    with patch("vibe.cli._task_request_store", return_value=request_store):
        result = cli.cmd_agent_run(args)
    captured = capsys.readouterr()
    return result, json.loads(captured.out or captured.err), request_store


def test_agent_run_bot_queues_on_the_bots_permanent_session(home, capsys):
    result, payload, store = _agent_run(home, capsys, ["--bot", "main", "--no-callback", "--message", "hello"])
    assert result == 0, payload
    assert payload["run"]["session_id"] == "ses_main"
    stored = store.get_run(payload["run"]["id"])
    assert stored["session_id"] == "ses_main"
    assert stored["metadata"]["delivery_intent"] == "queue"
    assert (home / "bots" / "main" / ".git").is_dir()

    result, payload, _store = _agent_run(home, capsys, ["--bot", "ghost", "--no-callback", "--message", "hi"])
    assert (result, payload["code"]) == (1, "bot_not_found")


@pytest.mark.parametrize(
    "extra",
    [["--model", "m"], ["--reasoning-effort", "low"], ["--agent", "vibey"], ["--session-id", "ses_main"], ["--fork-self"]],
)
def test_agent_run_bot_refuses_flags_that_pick_or_shape_a_session(home, capsys, extra):
    result, payload, _store = _agent_run(home, capsys, ["--bot", "main", *extra, "--no-callback", "--message", "hi"])
    assert (result, payload["code"]) == (1, "bot_run_conflict")


def test_agent_run_bot_without_a_caller_needs_a_callback_choice(home, capsys):
    result, payload, _store = _agent_run(home, capsys, ["--bot", "main", "--message", "hi"])
    assert (result, payload["code"]) == (1, "missing_async_callback")


def test_help_examples_parse_against_the_real_cli():
    """The examples in ``vibe bot --help`` and ``vibe agent run --help`` are live callers."""
    examples = re.findall(r"^\s+(vibe (?:bot|agent run) .+)$", cli._bot_examples_text() + cli._agent_run_examples_text(), re.M)
    assert sum(example.startswith("vibe bot ") for example in examples) >= 5
    parser = cli.build_parser()
    for example in examples:
        parser.parse_args(shlex.split(example)[1:])


@pytest.mark.parametrize("doc", ["docs/CLI.md", "docs/CLI_ZH.md"])
def test_cli_reference_bot_examples_parse(doc):
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / doc).read_text(encoding="utf-8")
    examples = [line for line in text.splitlines() if line.startswith("vibe bot ") or line.startswith("vibe agent run") and "--bot" in line]
    assert len(examples) >= 8
    parser = cli.build_parser()
    for example in examples:
        parser.parse_args(shlex.split(example)[1:])
