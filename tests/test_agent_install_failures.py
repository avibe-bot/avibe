"""Failed agent CLI installs and upgrades explain themselves.

The installer output fixtures below are real captures from npm 11, npm 9, curl
and ``opencode upgrade``, with only the machine-specific paths replaced.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest

from config.v2_config import V2Config
from vibe import api

NPM11_ENOTEMPTY = """\
npm error code ENOTEMPTY
npm error syscall rename
npm error path /Users/alice/.local/lib/node_modules/@openai/codex
npm error dest /Users/alice/.local/lib/node_modules/@openai/.codex-lD3lp9Ti
npm error errno -66
npm error ENOTEMPTY: directory not empty, rename '/Users/alice/.local/lib/node_modules/@openai/codex' -> '/Users/alice/.local/lib/node_modules/@openai/.codex-lD3lp9Ti'
npm error A complete log of this run can be found in: /Users/alice/.npm/_logs/2026-09-28T04_25_05_600Z-debug-0.log
"""

NPM9_ENOTEMPTY = """\
npm ERR! code ENOTEMPTY
npm ERR! syscall rename
npm ERR! path /usr/local/lib/node_modules/@openai/codex
npm ERR! dest /usr/local/lib/node_modules/@openai/.codex-RT5f2oYD
npm ERR! errno -66
npm ERR! ENOTEMPTY: directory not empty, rename '/usr/local/lib/node_modules/@openai/codex' -> '/usr/local/lib/node_modules/@openai/.codex-RT5f2oYD'

npm ERR! A complete log of this run can be found in: /Users/alice/.npm/_logs/2026-09-28T04_27_26_217Z-debug-0.log
"""


def _npm_permission(code: str, errno: str, message: str) -> str:
    return f"""\
npm error code {code}
npm error syscall mkdir
npm error path /usr/local/lib/node_modules/@openai
npm error errno {errno}
npm error Error: {code}: {message}, mkdir '/usr/local/lib/node_modules/@openai'
npm error     at async mkdir (node:internal/fs/promises:858:10)
npm error     at async Arborist.reify (/usr/local/lib/node_modules/npm/node_modules/@npmcli/arborist/lib/arborist/reify.js:125:5) {{
npm error   errno: {errno},
npm error   code: '{code}',
npm error   syscall: 'mkdir',
npm error   path: '/usr/local/lib/node_modules/@openai'
npm error }}
npm error
npm error The operation was rejected by your operating system.
npm error It is likely you do not have the permissions to access this file as the current user
npm error A complete log of this run can be found in: /Users/alice/.npm/_logs/2026-09-28T04_25_13_596Z-debug-0.log
"""


NPM_ENOTFOUND = """\
npm error code ENOTFOUND
npm error syscall getaddrinfo
npm error errno ENOTFOUND
npm error network request to https://registry.npmjs.org/@openai%2fcodex failed, reason: getaddrinfo ENOTFOUND registry.npmjs.org
npm error network This is a problem related to network connectivity.
npm error network In most cases you are behind a proxy or have bad network settings.
npm error A complete log of this run can be found in: /Users/alice/.npm/_logs/2026-09-28T04_25_20_730Z-debug-0.log
"""

NPM9_ENOTFOUND = NPM_ENOTFOUND.replace("npm error", "npm ERR!")

NPM_ECONNREFUSED = """\
npm error code ECONNREFUSED
npm error syscall connect
npm error errno ECONNREFUSED
npm error FetchError: request to http://127.0.0.1:7890/@openai%2fcodex failed, reason: connect ECONNREFUSED 127.0.0.1:7890
npm error     at ClientRequest.<anonymous> (/usr/local/lib/node_modules/npm/node_modules/minipass-fetch/lib/index.js:130:14) {
npm error   code: 'ECONNREFUSED',
npm error   errno: 'ECONNREFUSED',
npm error   syscall: 'connect',
npm error }
npm error If you are behind a proxy, please make sure that the
npm error 'proxy' config is set properly.  See: 'npm help config'
"""

NPM_ETIMEDOUT = """\
npm error code ETIMEDOUT
npm error syscall connect
npm error errno ETIMEDOUT
npm error network request to https://registry.npmjs.org/@openai%2fcodex failed, reason: connect ETIMEDOUT 104.16.0.35:443
npm error network This is a problem related to network connectivity.
"""

OPENCODE_UPGRADE_403 = """\
●  Using method: curl
Error: Unexpected error
StatusCode: non 2xx status code (403 GET https://api.github.com/repos/anomalyco/opencode/releases/latest)
"""


@pytest.fixture
def github_rate_limit(monkeypatch):
    """Answer ``GET /rate_limit`` at the HTTP boundary; record what was asked."""

    requests: list[tuple[str, float]] = []
    answer: dict = {"error": urllib.error.URLError("offline")}

    class Opener:
        def open(self, request, timeout):
            requests.append((request.full_url, timeout))
            if "error" in answer:
                raise answer["error"]
            return io.BytesIO(json.dumps(answer["payload"]).encode("utf-8"))

    monkeypatch.setattr(api, "_http_opener_for_best_effort_probe", Opener)
    return requests, answer


def _installer(tmp_path: Path, *, stderr: str, exit_code: int) -> list[str]:
    """A real installer process that prints what a failing installer prints."""

    (tmp_path / "installer.err").write_text(stderr, encoding="utf-8")
    script = tmp_path / "installer.sh"
    script.write_text(
        f'#!/bin/sh\necho "added 0 packages"\ncat "{tmp_path / "installer.err"}" >&2\nexit {exit_code}\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return [str(script)]


@pytest.mark.parametrize(
    ("output", "code", "params"),
    [
        pytest.param(
            NPM11_ENOTEMPTY,
            "npm_leftover_directory",
            {"path": "/Users/alice/.local/lib/node_modules/@openai/.codex-lD3lp9Ti"},
            id="npm11-enotempty",
        ),
        pytest.param(
            NPM9_ENOTEMPTY,
            "npm_leftover_directory",
            {"path": "/usr/local/lib/node_modules/@openai/.codex-RT5f2oYD"},
            id="npm9-enotempty",
        ),
        pytest.param(
            _npm_permission("EACCES", "-13", "permission denied"),
            "permission_denied",
            {"path": "/usr/local/lib/node_modules/@openai"},
            id="npm-eacces",
        ),
        pytest.param(
            _npm_permission("EACCES", "-13", "permission denied").replace("npm error", "npm ERR!"),
            "permission_denied",
            {"path": "/usr/local/lib/node_modules/@openai"},
            id="npm9-eacces",
        ),
        pytest.param(
            _npm_permission("EPERM", "-1", "operation not permitted"),
            "permission_denied",
            {"path": "/usr/local/lib/node_modules/@openai"},
            id="npm-eperm",
        ),
        pytest.param(OPENCODE_UPGRADE_403, "github_rate_limited", {"reset_at": None}, id="opencode-github-403"),
        pytest.param(NPM_ENOTFOUND, "network_unreachable", {"host": "registry.npmjs.org"}, id="npm-enotfound"),
        pytest.param(NPM9_ENOTFOUND, "network_unreachable", {"host": "registry.npmjs.org"}, id="npm9-enotfound"),
        pytest.param(NPM_ECONNREFUSED, "network_unreachable", {"host": "127.0.0.1"}, id="npm-econnrefused"),
        pytest.param(NPM_ETIMEDOUT, "network_unreachable", {"host": "registry.npmjs.org"}, id="npm-etimedout"),
        pytest.param(
            "curl: (6) Could not resolve host: claude.ai\n",
            "network_unreachable",
            {"host": "claude.ai"},
            id="curl-dns",
        ),
        pytest.param(
            "curl: (7) Failed to connect to opencode.ai port 443 after 0 ms: Couldn't connect to server\n",
            "network_unreachable",
            {"host": "opencode.ai"},
            id="curl-connect",
        ),
        pytest.param(
            "curl: (28) Failed to connect to claude.ai port 443 after 10002 ms: Timeout was reached\n",
            "network_unreachable",
            {"host": "claude.ai"},
            id="curl-connect-timeout",
        ),
        # A reachable server that refuses the download is not a network problem.
        pytest.param("curl: (56) The requested URL returned error: 404\n", "install_failed", {}, id="curl-http-404"),
        # npm renaming a retired copy back is not a leftover from an earlier run.
        pytest.param(
            "npm error ENOTEMPTY: directory not empty, rename "
            "'/usr/local/lib/node_modules/@openai/.codex-RT5f2oYD' -> '/usr/local/lib/node_modules/@openai/codex'\n",
            "install_failed",
            {},
            id="npm-enotempty-not-retire",
        ),
        pytest.param("Error: something unexpected happened\n", "install_failed", {}, id="unrecognized"),
        pytest.param(None, "install_failed", {}, id="no-output"),
    ],
)
def test_recognize_install_failure(github_rate_limit, output, code, params):
    requests, _answer = github_rate_limit

    assert api._recognize_install_failure(output) == (code, params)
    # Only a GitHub API refusal is worth asking GitHub about.
    assert bool(requests) == (code == "github_rate_limited")


def test_recognize_install_failure_reports_timeout_first():
    assert api._recognize_install_failure(NPM_ENOTFOUND, timed_out=True) == ("install_timeout", {"minutes": 5})


@pytest.mark.parametrize(
    ("language", "message", "hint"),
    [
        (
            "en",
            "Could not upgrade Codex.",
            "npm left a temporary folder behind from an earlier interrupted update: "
            "/Users/alice/.local/lib/node_modules/@openai/.codex-lD3lp9Ti. Delete that folder, then try again.",
        ),
        (
            "zh",
            "无法升级 Codex。",
            "npm 在之前一次中断的更新中留下了临时目录：/Users/alice/.local/lib/node_modules/@openai/.codex-lD3lp9Ti。删除该目录后重试。",
        ),
    ],
)
def test_failed_upgrade_names_the_leftover_npm_directory(tmp_path, language, message, hint):
    config = V2Config.default()
    config.language = language
    config.save()
    cmd = _installer(tmp_path, stderr=NPM11_ENOTEMPTY, exit_code=190)

    result = api._run_install_command("codex", cmd, lambda value: value, mode="upgrade")

    assert result["ok"] is False
    assert result["message"] == message
    assert result["code"] == "npm_leftover_directory"
    assert result["hint_params"] == {"path": "/Users/alice/.local/lib/node_modules/@openai/.codex-lD3lp9Ti"}
    assert result["hint"] == hint
    assert result["exit_code"] == 190
    assert result["reason"] == "codex_upgrade_failed"
    assert result["output"].startswith("added 0 packages")
    assert result["output"].endswith(NPM11_ENOTEMPTY.strip())


def test_unrecognized_failure_keeps_the_generic_message_and_output(tmp_path):
    cmd = _installer(tmp_path, stderr="Error: something unexpected happened\n", exit_code=3)

    result = api._run_install_command("claude", cmd, lambda value: value, mode="install")

    assert result["message"] == "Could not install Claude Code."
    assert result["code"] == "install_failed"
    assert result["hint"] is None
    assert result["exit_code"] == 3
    assert result["output"].startswith("added 0 packages")
    assert result["output"].endswith("Error: something unexpected happened")


@pytest.mark.parametrize("lookup", ["exhausted", "unavailable", "not-exhausted"])
def test_github_403_hint_says_when_the_limit_resets(tmp_path, github_rate_limit, lookup):
    requests, answer = github_rate_limit
    reset_at = int(time.time()) + 600
    if lookup != "unavailable":
        remaining = 0 if lookup == "exhausted" else 12
        answer.pop("error")
        answer["payload"] = {"resources": {"core": {"limit": 60, "remaining": remaining, "reset": reset_at}}}
    cmd = _installer(tmp_path, stderr=OPENCODE_UPGRADE_403, exit_code=1)

    result = api._run_install_command("opencode", cmd, lambda value: value, mode="upgrade")

    assert [url for url, _timeout in requests] == ["https://api.github.com/rate_limit"]
    assert all(timeout <= 5 for _url, timeout in requests)
    assert result["message"] == "Could not upgrade OpenCode."
    assert result["code"] == "github_rate_limited"
    if lookup == "exhausted":
        assert result["hint_params"] == {"reset_at": reset_at}
        assert result["hint"] == (
            "GitHub's API limit for this network is used up. It resets at "
            f"{datetime.fromtimestamp(reset_at).strftime('%H:%M')} (in about 10 min); try again after that."
        )
    else:
        assert result["hint_params"] == {"reset_at": None}
        assert result["hint"] == (
            "GitHub's API limit for this network is used up. It resets within an hour; try again later."
        )
    assert "403 GET https://api.github.com/repos/anomalyco/opencode/releases/latest" in result["output"]
