"""The metadata probe's opt-in Windows console policy reaches both processes."""

import asyncio
import io
import os
import sys
from types import SimpleNamespace

import pytest

from core import command_runner, watch_worker


@pytest.mark.parametrize("hidden", [False, True])
def test_supervisor_console_policy_is_opt_in_and_not_leaked(tmp_path, monkeypatch, hidden):
    seen = []
    original = command_runner.isolated_subprocess_kwargs

    def options(*, console=True):
        seen.append(console)
        return original(console=console)

    monkeypatch.setattr(command_runner, "isolated_subprocess_kwargs", options)
    result = asyncio.run(
        command_runner.run_supervised_command(
            command=[
                sys.executable,
                "-c",
                f'import os; print(os.environ.get({watch_worker.HIDE_CONSOLE_ENV!r}, "consumed"))',
            ],
            cwd=str(tmp_path),
            timeout_seconds=5,
            label="owned console contract",
            hide_console=hidden,
            max_output_bytes=1024,
            env={
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "PATH",
                    "SYSTEMROOT",
                    "WINDIR",
                    "COMSPEC",
                    "PATHEXT",
                    "TMPDIR",
                    "TEMP",
                    "TMP",
                    "HOME",
                    "AVIBE_HOME",
                    "CODEX_HOME",
                    "CLAUDE_CONFIG_DIR",
                    "XDG_CONFIG_HOME",
                    "XDG_DATA_HOME",
                    "XDG_CACHE_HOME",
                    "XDG_STATE_HOME",
                }
            },
        )
    )
    assert result.exit_code == 0 and result.stdout.strip() == "consumed"
    assert seen == [not hidden]


@pytest.mark.parametrize("hidden", [False, True])
def test_windows_worker_hides_actual_child_only_when_requested(monkeypatch, hidden):
    env = {watch_worker.PROCESS_IDENTITY_ENV: "owned-fixture-marker", "KEEP": "retained"}
    if hidden:
        env[watch_worker.HIDE_CONSOLE_ENV] = "1"
    monkeypatch.setattr(watch_worker, "os", SimpleNamespace(name="nt", environ=env))
    monkeypatch.setattr(watch_worker, "_install_posix_supervisor_signal_handlers", lambda: None)
    monkeypatch.setattr(watch_worker, "_install_windows_kill_on_close_job", lambda: object())
    monkeypatch.setattr(watch_worker, "_read_watch_worker_spec", lambda: (["fixture-cli", "--version"], None))
    monkeypatch.setattr(watch_worker, "_start_posix_identity_anchor", lambda: None)
    monkeypatch.setattr(watch_worker, "_stop_posix_identity_anchor", lambda _: None)
    monkeypatch.setattr(watch_worker, "_wait_for_posix_process_group_exit", lambda *_args, **_kwargs: None)
    calls = []

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=io.BytesIO(), stderr=io.BytesIO(), wait=lambda: 0)

    monkeypatch.setattr(
        watch_worker, "subprocess", SimpleNamespace(Popen=spawn, PIPE=-1, DEVNULL=-3, CREATE_NO_WINDOW=0x08000000)
    )
    assert watch_worker._run_watch_worker() == 0
    assert calls[0][1]["env"] == {"KEEP": "retained"}
    assert calls[0][1].get("creationflags", 0) == (0x08000000 if hidden else 0)
