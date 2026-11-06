"""Cold entry contracts: real parsing before any runtime or database dependency."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_ROOT = Path(__file__).resolve().parents[1]
# Captured from the pre-refactor parser (29bec5f70), not derived from the parser
# under test. COLUMNS is pinned below so usage wrapping is deterministic.
_MISSING_SUBCOMMANDS = [
    (["agent"], "{list,show,models,create,update,enable,disable,remove,import,run}",
     "usage: vibe agent [-h]\n                  {list,show,models,create,update,enable,disable,remove,import,run}\n                  ...", None),
    (["bot"], "{list,show,create,set,update,remove}",
     "usage: vibe bot [-h] {list,show,create,set,update,remove} ...", None),
    (["runs"], "{list,show,cancel}", "usage: vibe runs [-h] {list,show,cancel} ...", None),
    (["model-requests"], "{list,show,export}", "usage: vibe model-requests [-h] {list,show,export} ...", None),
    (["harness"], "{status}", "usage: vibe harness [-h] {status} ...", None),
    (["session"], "{list,get,queue,send-now,update}", "usage: vibe session [-h] {list,get,queue,send-now,update} ...",
     "Run one of the session subcommands below. Start with: vibe session list"),
    (["session", "queue"], "{list,remove}", "usage: vibe session queue [-h] {list,remove} ...", None),
    (["vault"], "{list,find,tags,edit,remove,run,fetch,access,sign,await,request,export,inject,key}",
     "usage: vibe vault [-h]\n                  {list,find,tags,edit,remove,run,fetch,access,sign,await,request,export,inject,key}\n                  ...",
     "Run one of the vault subcommands below. Start with: vibe vault list"),
    (["vault", "key"], "{export,import,authorize}", "usage: vibe vault key [-h] {export,import,authorize} ...",
     "Run: vibe vault key export  |  vibe vault key import <file>  |  vibe vault key authorize"),
    (["data"], "{query,cache,retention,skill-usage}", "usage: vibe data [-h] {query,cache,retention,skill-usage} ...", None),
    (["task"], "{add,update,list,show,pause,resume,run,remove}",
     "usage: vibe task [-h] {add,update,list,show,pause,resume,run,remove} ...",
     "Run one of the task subcommands below. Use 'vibe task add --help' for task creation details."),
    (["watch"], "{add,update,list,show,pause,resume,remove}",
     "usage: vibe watch [-h] {add,update,list,show,pause,resume,remove} ...",
     "Run one of the watch subcommands below. Use 'vibe watch add --help' for watch creation details."),
]
_STATIC_ERRORS = [
    (argv, {
        "schema_version": 1, "ok": False, "kind": "error", "code": "invalid_arguments",
        "error": f"the following arguments are required: {choices}", "usage": usage,
        **({"hint": hint} if hint else {}), "help_command": f"vibe {' '.join(argv)} --help",
    })
    for argv, choices, usage, hint in _MISSING_SUBCOMMANDS
]
# A fresh interpreter is essential: conftest and the existing CLI tests have
# already imported these modules before their assertions run.
_PROBE = r'''
import importlib.abc
import json
import sqlite3
import sys

forbidden = (
    "sqlalchemy", "alembic", "apscheduler", "vibe.api", "vibe.runtime",
    "vibe.upgrade", "vibe.restart_supervisor", "core.scheduled_tasks",
    "core.watches", "storage.db", "storage.importer", "config.v2_config",
    "modules.agents.base", "modules.agents.service",
)
attempts = []
class NoHeavyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
            attempts.append(fullname)
            raise AssertionError("cold entry imported " + fullname)

def no_database(*args, **kwargs):
    attempts.append("sqlite3.connect")
    raise AssertionError("cold entry opened SQLite")

def audit(event, args):
    if event == "sqlite3.connect":
        no_database()
    if event in {"subprocess.Popen", "socket.connect"}:
        attempts.append(event)
        raise AssertionError("cold entry performed external I/O: " + event)

sqlite3.connect = no_database
sys.addaudithook(audit)
sys.meta_path.insert(0, NoHeavyImports())
argv = json.loads(sys.argv[1])
mode = sys.argv[2]
try:
    from vibe import cli
    if mode == "parse":
        args = cli.build_parser().parse_args(argv)
        print(json.dumps(vars(args)))
    elif argv is not None:
        if mode == "screenshot":
            # A real failure branch without observing the developer's desktop.
            sys.platform = "cold-entry-test"
        sys.argv = ["vibe", *argv]
        cli.main()
finally:
    # A broad product exception handler must not conceal a forbidden attempt.
    assert not attempts, attempts
'''


def _cold_probe(tmp_path, argv, *, remote=False, mode="main"):
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AVIBE_", "VIBE_"))
    }
    for name in (
        "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
        "XDG_STATE_HOME", "AVIBE_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    ):
        env[name] = str(tmp_path / name.lower())
    if remote:
        env["AVIBE_CALLER_REMOTE"] = "true"
        env["AVIBE_CALLER_RESOURCE_CONTEXT"] = json.dumps({
            "instance_role": "owner", "instance_kind": "personal", "instance_id": "untrusted",
        })
    env["COLUMNS"] = "80"
    env["PYTHONPATH"] = str(_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(argv), mode],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30,
    )


@pytest.mark.parametrize("remote", [False, True], ids=["local", "declared-remote"])
@pytest.mark.parametrize(
    ("argv", "exit_code", "output"),
    [
        (None, 0, "import"),
        (["version"], 0, "version"),
        (["version", "--json"], 0, "version-json"),
        (["--help"], 0, "help"),
        (["task", "add", "--help"], 0, "help"),
        (["vault", "fetch", "--help"], 0, "help"),
        (["data", "retention", "--help"], 0, "help"),
        (["show"], 0, "help"),
        (["not-a-command"], 2, "error"),
        (["restart", "--delay-seconds", "nan"], 2, "error"),
        (["agent", "create", "example", "--backend", "unknown"], 2, "error"),
        (["agent", "create"], 2, "error"),
        (["--version"], 2, "unsupported-version"),
    ] + [(argv, 2, expected) for argv, expected in _STATIC_ERRORS],
)
def test_static_entry_never_imports_runtime_or_opens_database(tmp_path, remote, argv, exit_code, output):
    result = _cold_probe(tmp_path, argv, remote=remote)
    assert result.returncode == exit_code, result.stderr
    if isinstance(output, dict):
        assert result.stdout == ""
        assert json.loads(result.stderr) == output
    elif output in {"error", "unsupported-version"}:
        assert result.stdout == ""
        payload = json.loads(result.stderr)
        if output == "unsupported-version":
            assert payload["error"] == "unrecognized arguments: --version"
        if argv == ["restart", "--delay-seconds", "nan"]:
            assert payload["error"] == "argument --delay-seconds: must be finite"
            assert payload["usage"] == "usage: vibe restart [-h] [--delay-seconds DELAY_SECONDS] [--allow-downgrade]"
        assert payload["schema_version"] == 1
        assert payload["ok"] is False
        assert payload["kind"] == "error"
        assert payload["code"] == "invalid_arguments"
        assert payload["usage"].startswith("usage: vibe")
    else:
        assert result.stderr == ""
        if output == "import":
            assert result.stdout == ""
        elif output == "help":
            assert result.stdout.startswith("usage: vibe")
        elif output == "version":
            assert result.stdout.startswith("avibe-os ")
        else:
            payload = json.loads(result.stdout)
            assert set(payload) == {"schema_version", "ok", "kind", "version"}
            assert payload["schema_version"] == 1
            assert payload["ok"] is True
            assert payload["kind"] == "version"
    assert not list(tmp_path.rglob("*.sqlite*"))


@pytest.mark.parametrize(
    ("argv", "attribute", "tail"),
    [
        (["task", "add", "--cron", "0 * * * *", "--", "echo", "--help"],
         "command_argv", ["--", "echo", "--help"]),
        (["task", "update", "task1", "--", "echo", "--help"],
         "command_argv", ["--", "echo", "--help"]),
        (["watch", "update", "watch1", "--", "echo", "--help"],
         "waiter_command", ["--", "echo", "--help"]),
    ],
)
def test_command_tails_remain_data_in_cold_parser(tmp_path, argv, attribute, tail):
    result = _cold_probe(tmp_path, argv, mode="parse")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload[attribute] == tail
    assert payload["command"] == argv[0]


@pytest.mark.parametrize("argv", [["mcp", "--help"], ["mcp-add", "--help"], ["mcp-discover", "--help"]])
def test_selected_mcp_help_does_not_import_unrelated_handlers(tmp_path, argv):
    # Real raw dispatcher, admission and MCP handler: no mocked command function.
    # Remote raw entry keeps its existing identity-before-help contract instead.
    result = _cold_probe(tmp_path, argv)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.startswith("usage:")


def test_selected_screenshot_failure_does_not_import_other_command_families(tmp_path):
    result = _cold_probe(tmp_path, ["screenshot", "--output", "capture.png"], mode="screenshot")
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    assert result.stderr == "Screenshot failed: screenshots are not supported on this platform: cold-entry-test\n"
    assert not list(tmp_path.rglob("*.sqlite*"))
    assert not (tmp_path / "capture.png").exists()
