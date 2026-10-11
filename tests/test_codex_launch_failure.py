"""MH-CODEX-LAUNCH-003: a Codex CLI that cannot start asks for a repair, not a retry.

The launcher captures are real stderr from the published npm launchers
(@openai/codex 0.121.0 and 0.162.1) in disposable incomplete installs, with
only the machine-specific prefix replaced. The Codex captures are the CLI's
own failures, which keep their ordinary handling. Every process below is real:
the export runs through the stable supervisor and the app-server through its
transport, so recognition is proved where Avibe actually observes the failure.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

from core.native_dispatch_phase import (
    DISPATCH_PHASE_PREWRITE,
    prewrite_failure_evidence,
    set_dispatch_phase,
)
from modules.agents.codex.transport import CodexTransport
from tests.test_codex_runtime_generations import _Router, _agent, _launch, _request
from vibe import backend_model_catalog
from vibe.codex_launch_failure import (
    CLI_NOT_FOUND,
    INSTALL_INCOMPLETE,
    PERMISSION_DENIED,
    CodexLaunchError,
    launch_failure_from_stderr,
)
from vibe.i18n import t as i18n_t

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX launcher fixtures")

PREFIX = "/opt/codex-fixture/lib/node_modules/@openai/codex"
NATIVE_0_121 = f"{PREFIX}/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/codex/codex"
NATIVE_0_162 = f"{PREFIX}/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"


def _node_spawn_failure(native: str, code: str, errno: int) -> str:
    return f"""\
Error: spawn {native} {code}
    at ChildProcess._handle.onexit (node:internal/child_process:285:19)
    at onErrorNT (node:internal/child_process:483:16)
    at process.processTicksAndRejections (node:internal/process/task_queues:90:21) {{
  errno: -{errno},
  code: '{code}',
  syscall: 'spawn {native}',
  path: '{native}',
  spawnargs: [ 'debug', 'models', '--bundled', '-c', 'model_catalog_json=null' ]
}}
"""


# 0.121.0, platform package present, native executable missing.
NATIVE_MISSING_0_121 = _node_spawn_failure(NATIVE_0_121, "ENOENT", 2)
# 0.162.1, native executable present but not executable.
NATIVE_NOT_EXECUTABLE_0_162 = _node_spawn_failure(NATIVE_0_162, "EACCES", 13)
# 0.121.0, platform package missing.
PLATFORM_MISSING_0_121 = f"""\
file://{PREFIX}/bin/codex.js:100
    throw new Error(
          ^

Error: Missing optional dependency @openai/codex-darwin-arm64. Reinstall Codex: npm install -g @openai/codex@latest
    at file://{PREFIX}/bin/codex.js:100:11
    at ModuleJob.run (node:internal/modules/esm/module_job:343:25)
    at async onImport.tracePromise.__proto__ (node:internal/modules/esm/loader:647:26)
    at async asyncRunEntryPointWithESMLoader (node:internal/modules/run_main:117:5)

Node.js v22.18.0
"""
# 0.162.1, platform package or its native executable missing.
NATIVE_MISSING_0_162 = f"""\
file://{PREFIX}/bin/codex.js:107
  throw new Error(
        ^

Error: Missing optional dependency @openai/codex-darwin-arm64. Reinstall Codex: npm install -g @openai/codex@latest
    at findCodexExecutable (file://{PREFIX}/bin/codex.js:107:9)
    at file://{PREFIX}/bin/codex.js:112:20
    at ModuleJob.run (node:internal/modules/esm/module_job:343:25)
    at async onImport.tracePromise.__proto__ (node:internal/modules/esm/loader:647:26)
    at async asyncRunEntryPointWithESMLoader (node:internal/modules/run_main:117:5)

Node.js v22.18.0
"""
# Codex 0.162.1's own failures: a missing catalog file, and a usage error.
CODEX_MISSING_FILE = "Error: No such file or directory (os error 2)\n"
CODEX_USAGE = """\
error: unexpected argument '--unknown-flag' found

Usage: codex debug models --bundled

For more information, try '--help'.
"""
# The launcher's interpreter is not on PATH (exit 127).
NODE_MISSING = "env: node: No such file or directory\n"

# Stands for anything a process prints: it must never leave recognition.
SENTINEL = "printed-only-to-stderr-5c1e"


@pytest.mark.parametrize(
    ("stderr", "cause"),
    [
        (NATIVE_MISSING_0_121, INSTALL_INCOMPLETE),
        (PLATFORM_MISSING_0_121, INSTALL_INCOMPLETE),
        (NATIVE_MISSING_0_162, INSTALL_INCOMPLETE),
        (NATIVE_MISSING_0_162.replace("\n", "\r\n"), INSTALL_INCOMPLETE),
        (NATIVE_NOT_EXECUTABLE_0_162, PERMISSION_DENIED),
        (CODEX_MISSING_FILE, None),
        (CODEX_USAGE, None),
        (NODE_MISSING, None),
        ("Error: ENOENT: no such file or directory, open '/opt/codex-fixture/config.toml'\n", None),
        ("", None),
    ],
)
def test_recognizes_only_launch_failures_it_has_evidence_for(stderr, cause):
    """MH-CODEX-LAUNCH-003: only the launcher's own reports name a repair cause."""

    assert launch_failure_from_stderr(stderr) == cause


def _replaying_cli(tmp_path, stderr: str, *, code: int = 1) -> str:
    """An executable that fails the way a captured launch did."""

    script = tmp_path / "codex"
    script.write_text(f"#!/bin/sh\ncat >&2 <<'CAPTURE'\n{stderr}{SENTINEL}\nCAPTURE\nexit {code}\n")
    script.chmod(0o755)
    return str(script)


def _missing_cli(tmp_path) -> str:
    return str(tmp_path / "absent" / "codex")


def _unexecutable_cli(tmp_path) -> str:
    script = tmp_path / "codex"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o644)
    return str(script)


UNRUNNABLE = [
    pytest.param(lambda path: _replaying_cli(path, NATIVE_MISSING_0_162), INSTALL_INCOMPLETE, id="platform-missing"),
    pytest.param(lambda path: _replaying_cli(path, NATIVE_MISSING_0_121), INSTALL_INCOMPLETE, id="native-missing"),
    pytest.param(
        lambda path: _replaying_cli(path, NATIVE_NOT_EXECUTABLE_0_162), PERMISSION_DENIED, id="native-not-executable",
    ),
    pytest.param(_missing_cli, CLI_NOT_FOUND, id="cli-missing"),
    pytest.param(_unexecutable_cli, PERMISSION_DENIED, id="cli-not-executable"),
]
ORDINARY = [
    pytest.param(CODEX_MISSING_FILE, 1, id="codex-error"),
    pytest.param(CODEX_USAGE, 2, id="codex-usage"),
    pytest.param(NODE_MISSING, 127, id="node-missing"),
]


@pytest.mark.parametrize(("make_cli", "cause"), UNRUNNABLE)
def test_catalog_export_reports_why_codex_cannot_start(tmp_path, make_cli, cause):
    """MH-CODEX-LAUNCH-003: the supervised export names the cause, never the output."""

    with pytest.raises(CodexLaunchError) as raised:
        backend_model_catalog._export_codex_bundled_catalog(make_cli(tmp_path))

    assert raised.value.cause == cause
    assert SENTINEL not in repr(raised.value) and str(tmp_path) not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None


@pytest.mark.parametrize(("stderr", "code"), ORDINARY)
def test_catalog_export_keeps_other_failures_retryable(tmp_path, stderr, code):
    with pytest.raises(RuntimeError, match="could not export its bundled model catalog") as raised:
        backend_model_catalog._export_codex_bundled_catalog(_replaying_cli(tmp_path, stderr, code=code))

    assert not isinstance(raised.value, CodexLaunchError)
    assert SENTINEL not in repr(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_cli", "cause"), UNRUNNABLE)
async def test_app_server_start_reports_why_codex_cannot_start(tmp_path, make_cli, cause):
    """MH-CODEX-LAUNCH-003: an app-server that cannot start names its cause."""

    transport = CodexTransport(binary=make_cli(tmp_path), cwd=str(tmp_path / "work"))

    with pytest.raises(CodexLaunchError) as raised:
        await transport.start()

    assert raised.value.cause == cause
    assert SENTINEL not in str(raised.value)
    assert not transport.is_alive


@pytest.mark.asyncio
@pytest.mark.parametrize(("stderr", "code"), ORDINARY)
async def test_app_server_start_keeps_other_failures(tmp_path, stderr, code):
    transport = CodexTransport(binary=_replaying_cli(tmp_path, stderr, code=code), cwd=str(tmp_path / "work"))

    with pytest.raises(ConnectionError):
        await transport.start()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["hub", "direct"])
@pytest.mark.parametrize(("make_cli", "cause"), UNRUNNABLE)
async def test_a_codex_cli_that_cannot_start_holds_the_input_for_a_repair(tmp_path, channel, make_cli, cause):
    """MH-CODEX-LAUNCH-003: the turn asks for a repair and keeps its input for an explicit retry.

    A Hub turn fails while exporting the catalog; a direct turn while starting
    the app-server. Neither may tell the user to just retry, start a Hub
    cooldown, or leave a generation behind for the next turn.
    """

    router = _Router()
    router.launches["model-a"] = _launch("model-a", channel, token="hub-token")
    agent, cwd = _agent(tmp_path, router=router)
    agent.codex_config.binary = make_cli(tmp_path)
    agent.controller.emit_agent_message = AsyncMock(return_value="notice-1")
    request = _request(cwd, "s1")
    set_dispatch_phase(request.context, DISPATCH_PHASE_PREWRITE)

    await agent.handle_message(request)

    assert prewrite_failure_evidence(request.context) == {
        "reason": f"codex_cli_{cause}",
        "requires_explicit_retry": True,
    }
    notify = agent.controller.emit_agent_message.await_args_list[0]
    assert notify.args[1:3] == ("notify", f"❌ {i18n_t(CodexLaunchError(cause).copy_key, 'en')}")
    result = agent.controller.emit_agent_message.await_args_list[-1]
    assert result.args[1] == "result" and result.kwargs["is_error"] is True
    assert SENTINEL not in repr(agent.controller.emit_agent_message.await_args_list)
    agent._record_model_hub_native_failure.assert_not_awaited()
    assert agent._units[cwd].current is None
