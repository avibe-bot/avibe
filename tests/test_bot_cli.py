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


def test_a_bot_message_from_a_bot_has_no_callback_and_names_its_sender(home, capsys, monkeypatch):
    """bots.md D4 (2026-11-02): `--bot` delivers a message, queued, with no callback; the Bot reads its sender."""
    import asyncio
    from types import SimpleNamespace

    from core.agent_input import AgentInputMetadata
    from core.handlers.message_handler import MessageHandler
    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore, parse_session_key

    from core.bots import BotService

    with BotService() as bots:
        bots.ensure_main_bot()
        bots.create_bot(display_name="Security", handle="cso")
    # The main Bot's own shell: an Agent run there defaults its callback to the caller, which --bot must not.
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses_main")
    result, payload, store = _agent_run(home, capsys, ["--bot", "cso", "--message", "Review the login flow."])
    assert result == 0, payload
    assert "callback_session_id" not in payload["run"] and "callback_notice" not in payload
    stored = store.get_run(payload["run"]["id"])
    assert (stored["session_id"], stored["callback_session_id"], stored["callback_status"]) == ("ses_cso", None, None)
    assert stored["metadata"]["delivery_intent"] == "queue"
    assert (stored["source_kind"], stored["source_actor"]) == ("agent", "ses_main")

    # The input the cso Bot receives, through the scheduler's own dispatch context for this run.
    controller = SimpleNamespace(
        platform_settings_managers={"slack": SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_a, **_k: None))},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True, should_use_thread_for_dm_session=lambda: False,
        ),
    )
    service = ScheduledTaskService(controller=controller, store=ScheduledTaskStore(home / "scheduled.json"))
    context = asyncio.run(service._build_context(
        parse_session_key("slack::channel::C123"),
        execution_id=stored["id"],
        task_id=None,
        trigger_kind="agent_run",
        metadata={**(stored["metadata"] or {}), "source_kind": stored["source_kind"],
                  "source_actor": stored["source_actor"], "parent_run_id": stored["parent_run_id"],
                  "callback_session_id": stored["callback_session_id"]},
    ))
    prepared = asyncio.run(MessageHandler.prepare_input_metadata(object(), context, human=False))
    assert isinstance(prepared, AgentInputMetadata)
    first_line = prepared.render("Review the login flow.", SimpleNamespace(include_time_info=False)).splitlines()[0]
    assert first_line == f"[Harness: agent run {stored['id']} from @<bot:main>]"


@pytest.mark.parametrize(
    "extra", [["--callback-session-id", "sesabc1234567"], ["--sync"], ["--sync", "--wait-timeout", "5"]]
)
def test_agent_run_bot_refuses_waiting_for_a_reply(home, capsys, extra):
    result, payload, _store = _agent_run(home, capsys, ["--bot", "main", *extra, "--message", "hi"])
    assert (result, payload["code"]) == (1, "bot_run_no_callback")


def test_agent_run_bot_needs_no_callback_policy_outside_an_agent_shell(home, capsys):
    result, payload, store = _agent_run(home, capsys, ["--bot", "main", "--message", "hi"])
    assert result == 0, payload
    assert store.get_run(payload["run"]["id"])["callback_session_id"] is None
