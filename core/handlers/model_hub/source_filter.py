"""Settings › Experimental › Source filter (``docs/plans/vibey-source-filter-hooks.md``).

While the experiment is on, a Vibey turn's ``web_search`` and ``web_fetch`` run only through the gateway (the main
model never searches natively, ``hosted_tools.freeze_turn_tools``), and each logical call passes the owner's hook
scripts in ``$AVIBE_HOME/hooks/source_filter/``:

- ``before_search.py`` / ``before_fetch.py`` see the model's arguments before any candidate is tried. They may pass,
  replace the arguments, answer with their own output (nothing is sent upstream), or block the call.
- ``after_search.py`` / ``after_fetch.py`` see the call's final output, success or failure, before the model, the
  history and the card do. They may pass, replace the whole output, or block it.

Each script is optional and is looked up when its hook point is reached: a missing file is skipped. A script that
is found but fails (non-zero exit, timeout, oversized or invalid output) blocks the call; the unfiltered output is
never released and nothing is retried. Candidate retries and fallbacks inside the chain never run a hook again.

The scripts own what the model reads, without allow-lists. Avibe still owns the facts: what ran upstream, which
candidate served, and what it cost stay on the record's attempts and the call's execution, whatever a hook returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Final, Mapping, Optional

from config import paths
from core.agent_core.messages import HOSTED_ERROR_MAX_CHARS

logger = logging.getLogger(__name__)

PROTOCOL_VERSION: Final = 1
SOURCE_FILTER_TOOLS: Final = ("web_search", "web_fetch")
HOOKS: Final[Mapping[str, tuple[str, str]]] = {
    "web_search": ("before_search", "after_search"),
    "web_fetch": ("before_fetch", "after_fetch"),
}
HOOK_NAMES: Final = ("before_search", "after_search", "before_fetch", "after_fetch")
# One hook run, inside the call. A hook may call a model, so it is not a few seconds.
HOOK_TIMEOUT_SECONDS: Final = 60.0
STDOUT_MAX_BYTES: Final = 4 * 1024 * 1024
_STDERR_KEPT_BYTES: Final = 4 * 1024
_READ_CHUNK: Final = 64 * 1024
REASON_MAX_CHARS: Final = 300
# What a hook may put into a record: the bounds the search mapping and the fetch record already keep.
_MAX_RESULTS: Final = 50
_SOURCE_BOUNDS: Final = (("title", 300), ("snippet", 1000), ("page_age", 64))
_URL_MAX: Final = 2048
_FETCH_URL_MAX: Final = 8192
_FETCH_TITLE_MAX: Final = 300
_FETCH_ERROR_MAX: Final = 1000
# The variables a hook inherits; everything else (provider keys, tokens) stays in Avibe's environment.
_INHERITED_ENV: Final = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)
_CODE_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def hook_dir() -> Path:
    return paths.get_vibe_remote_dir() / "hooks" / "source_filter"


def _venv_python(directory: Path) -> Path:
    return directory / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _locate(name: str) -> tuple[Path, str] | None:
    """The script for a hook and the interpreter that runs it, or None when the script is not there."""

    directory = hook_dir()
    script = directory / f"{name}.py"
    if not script.is_file():
        return None
    venv = _venv_python(directory)
    # The hook folder's own environment (``uv sync --locked``) when it has one; else Avibe's Python.
    return script, str(venv) if venv.is_file() else sys.executable


def hook_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _INHERITED_ENV if key in os.environ}
    env["AVIBE_HOME"] = str(paths.get_vibe_remote_dir())
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


class HookFailed(Exception):
    """A script that was found and run but did not answer within the protocol."""


@dataclass(frozen=True)
class HookAnswer:
    """``None`` when the script was not there (skipped); else its validated response."""

    response: Optional[dict[str, Any]]


def _code(value: object) -> Optional[str]:
    if isinstance(value, str) and 0 < len(value) <= 64 and value[0].isalpha() and set(value) <= _CODE_CHARS:
        return value
    return None


def _validate(name: str, response: object) -> dict[str, Any]:
    """The response if it follows the protocol; ``HookFailed`` naming what is wrong otherwise."""

    if not isinstance(response, dict):
        raise HookFailed("the response is not a JSON object")
    action = response.get("action")
    before = name.startswith("before_")
    allowed = {"pass", "replace", "respond", "block"} if before else {"pass", "replace", "block"}
    if not isinstance(action, str) or action not in allowed:
        raise HookFailed(f"unknown action {str(action)[:40]!r}")
    if action == "replace" and before and not isinstance(response.get("args"), dict):
        raise HookFailed("replace needs an args object")
    if action == "respond" or (action == "replace" and not before):
        _validate_output(response.get("output"))
    if action == "block" and response.get("reason") is not None and not isinstance(response.get("reason"), str):
        raise HookFailed("reason must be a string")
    return response


def _validate_output(output: object) -> None:
    if not isinstance(output, dict) or not isinstance(output.get("ok"), bool):
        raise HookFailed("output needs a boolean ok")
    if output["ok"]:
        if not isinstance(output.get("result"), dict):
            raise HookFailed("a successful output needs a result object")
    else:
        error = output.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("message"), str) or not error["message"].strip():
            raise HookFailed("a failed output needs an error object with a message")


async def _exchange(process: asyncio.subprocess.Process, data: bytes) -> tuple[bytes, bytes]:
    """Write the request, read the answer within its cap, and keep the last of stderr; the process then ends."""

    async def write() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # A script that ignores its input still answers; its output decides.
            pass
        finally:
            process.stdin.close()

    async def read_out() -> bytes:
        assert process.stdout is not None
        chunks: list[bytes] = []
        size = 0
        while chunk := await process.stdout.read(_READ_CHUNK):
            size += len(chunk)
            if size > STDOUT_MAX_BYTES:
                raise HookFailed(f"the output is larger than {STDOUT_MAX_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    async def read_err() -> bytes:
        assert process.stderr is not None
        tail = b""
        while chunk := await process.stderr.read(_READ_CHUNK):
            tail = (tail + chunk)[-_STDERR_KEPT_BYTES:]
        return tail

    writing = asyncio.ensure_future(write())
    erring = asyncio.ensure_future(read_err())
    try:
        out = await read_out()
        await writing
        err = await erring
        await process.wait()
    finally:
        for task in (writing, erring):
            if not task.done():
                task.cancel()
    return out, err


class SourceFilterHooks:
    """Runs the hook scripts and keeps each hook's last failure for Settings (in memory: a restart clears it)."""

    def __init__(self, *, timeout: float = HOOK_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self._failures: dict[str, dict[str, str]] = {}

    def status(self) -> dict[str, Any]:
        """What Settings › Experimental › Source filter shows: the folder and each script's state."""

        directory = hook_dir()
        hooks = {}
        for name in HOOK_NAMES:
            found = (directory / f"{name}.py").is_file()
            failure = self._failures.get(name) if found else None
            hooks[name] = {"found": found, "last_failure": dict(failure) if failure else None}
        return {"dir": str(directory), "hooks": hooks}

    async def run(self, name: str, request: Mapping[str, Any]) -> HookAnswer:
        located = await asyncio.to_thread(_locate, name)
        if located is None:
            return HookAnswer(None)
        script, python = located
        try:
            response = await self._run(name, script, python, request)
        except HookFailed as failed:
            reason = str(failed)
            self._failures[name] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reason": reason}
            logger.warning("Source filter hook %s failed: %s", name, reason)
            raise
        self._failures.pop(name, None)
        return HookAnswer(response)

    async def _run(self, name: str, script: Path, python: str, request: Mapping[str, Any]) -> dict[str, Any]:
        # A fetch request carries a whole page: encoded off the controller loop.
        data = await asyncio.to_thread(lambda: json.dumps(request, ensure_ascii=False).encode("utf-8"))
        try:
            # Its own process group (session), so whatever the script starts ends with the call.
            process = await asyncio.create_subprocess_exec(
                python, str(script),
                cwd=str(script.parent),
                env=hook_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_own_group(),
            )
        except OSError as error:
            raise HookFailed(f"it could not start ({type(error).__name__})") from None
        try:
            out, err = await asyncio.wait_for(_exchange(process, data), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise HookFailed(f"it timed out after {int(self.timeout)} s") from None
        finally:
            # On every path, a stopped turn included: nothing the script started outlives the call.
            await _kill(process)
        if err:
            logger.info("Source filter hook %s stderr: %s", name, err.decode("utf-8", "replace")[-1000:])
        if process.returncode != 0:
            raise HookFailed(f"it exited with code {process.returncode}")
        try:
            response = await asyncio.to_thread(lambda: json.loads(out.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise HookFailed("its output is not one UTF-8 JSON object") from None
        return _validate(name, response)


def _own_group() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


async def _kill(process: asyncio.subprocess.Process) -> None:
    """End the hook and every process it started (its process group; the process tree on Windows)."""

    if os.name == "nt":
        if process.returncode is None:
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(process.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=5)
            except (OSError, asyncio.TimeoutError):
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Source filter hook process %s did not exit after kill", process.pid)


# ---------------------------------------------------------------------------------------------------------------
# The canonical answer and the protocol's output, both ways.


def output_of(tool: str, answer: Mapping[str, Any]) -> dict[str, Any]:
    """The protocol output for a gateway answer: what the model would read."""

    record = answer.get("record") or {}
    if record.get("status") == "completed":
        if tool == "web_search":
            return {"ok": True, "result": {"results": [
                {key: value for key, value in source.items() if key in ("url", "title", "snippet", "page_age")}
                for source in record.get("sources") or () if isinstance(source, Mapping)
            ]}}
        return {"ok": True, "result": {
            "url": record.get("requested_url"),
            "final_url": record.get("final_url"),
            "title": record.get("title"),
            "content": record.get("content", ""),
        }}
    return {"ok": False, "error": {
        "code": record.get("error_code") or ("search_failed" if tool == "web_search" else "fetch_unavailable"),
        "message": record.get("error") or "The call failed.",
    }}


def _text(value: object, bound: int) -> Optional[str]:
    return value[:bound] if isinstance(value, str) else None


def search_record(output: Mapping[str, Any], *, attempts: object = None) -> dict[str, Any]:
    """A ``HostedRecord`` payload for a protocol output. Any source the script names is kept, within the bounds."""

    record: dict[str, Any]
    if output["ok"]:
        results = output["result"].get("results")
        if not isinstance(results, list):
            raise HookFailed("a search result needs a results array")
        sources = []
        for item in results[:_MAX_RESULTS]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not item["url"].strip():
                raise HookFailed("each search result needs a url")
            source = {"url": item["url"][:_URL_MAX]}
            for field, bound in _SOURCE_BOUNDS:
                if (value := _text(item.get(field), bound)) is not None:
                    source[field] = value
            sources.append(source)
        record = {"status": "completed", "sources": sources}
    else:
        error = output["error"]
        record = {"status": "failed", "error": error["message"][:HOSTED_ERROR_MAX_CHARS], "sources": []}
        if (code := _code(error.get("code"))) is not None:
            record["error_code"] = code
    if isinstance(attempts, list):
        record["attempts"] = attempts
    return record


def fetch_record_for(output: Mapping[str, Any], *, requested_url: str, attempts: object = None) -> dict[str, Any]:
    """A ``WebFetchRecord`` payload for a protocol output: the script may name any URL and content."""

    from .fetch_chain import fetch_record

    if output["ok"]:
        result = output["result"]
        content = result.get("content")
        if not isinstance(content, str):
            raise HookFailed("a fetch result needs content text")
        url = _text(result.get("url"), _FETCH_URL_MAX) or requested_url[:_FETCH_URL_MAX]
        raw = {
            "status": "completed",
            "content": content,
            "final_url": _text(result.get("final_url"), _FETCH_URL_MAX),
            "title": _text(result.get("title"), _FETCH_TITLE_MAX),
        }
    else:
        error = output["error"]
        url = requested_url[:_FETCH_URL_MAX]
        message = error["message"][:_FETCH_ERROR_MAX]
        raw = {"status": "failed", "error": message, "content": message, "error_code": _code(error.get("code"))}
    record = fetch_record(raw, requested_url=url)
    if isinstance(attempts, list):
        record["attempts"] = attempts
    return record


# The executor a hook-written successful output names when nothing upstream produced one: an Avibe-owned origin with
# released enum values (an older release reads the record), and no charge, because nothing upstream ran.
SOURCE_FILTER_SOURCE_ID: Final = "src_sourcefilter"


def hook_execution(tool: str, hook: str) -> dict[str, Any]:
    return {
        "mode": "proxy",
        "executor_origin": {"provider": "avibe", "api": "search_api" if tool == "web_search" else "fetch_api", "model": hook},
        "executor_source_id": SOURCE_FILTER_SOURCE_ID,
        "executor_billing": "metered",
        "cost": {"basis": "reported", "usd": 0.0},
    }


def record_for(tool: str, output: Mapping[str, Any], *, requested_url: str, attempts: object = None) -> dict[str, Any]:
    if tool == "web_search":
        return search_record(output, attempts=attempts)
    return fetch_record_for(output, requested_url=requested_url, attempts=attempts)


# ---------------------------------------------------------------------------------------------------------------
# One filtered call.

Runner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Say = Callable[..., str]


async def filtered_call(
    hooks: SourceFilterHooks,
    tool: str,
    arguments: dict[str, Any],
    run: Runner,
    say: Say,
) -> dict[str, Any]:
    """One logical ``web_search`` / ``web_fetch`` call between its two hooks.

    ``run`` is the unfiltered gateway call for a set of arguments; it answers the canonical ``{"record",
    "execution"}``. A hook's failure blocks the call: the model gets an error record, never the unfiltered output.
    """

    before, after = HOOKS[tool]
    url_of = (lambda args: str(args.get("url") or "")) if tool == "web_fetch" else (lambda args: "")

    def blocked(code: str, text: str, args: Mapping[str, Any], execution: object = None, attempts: object = None):
        output = {"ok": False, "error": {"code": code, "message": text}}
        answer: dict[str, Any] = {"record": record_for(tool, output, requested_url=url_of(args), attempts=attempts)}
        if execution is not None:
            answer["execution"] = execution
        return answer

    def failed(hook: str, error: HookFailed, args, execution=None, attempts=None):
        return blocked(
            "source_filter_failed", say("modelHub.sourceFilter.failed", hook=f"{hook}.py", reason=str(error)),
            args, execution, attempts,
        )

    def block_text(response: Mapping[str, Any]) -> str:
        reason = response.get("reason")
        if isinstance(reason, str) and reason.strip():
            return say("modelHub.sourceFilter.blocked", reason=reason.strip()[:REASON_MAX_CHARS])
        return say("modelHub.sourceFilter.blockedNoReason")

    args = dict(arguments)
    answer: Optional[dict[str, Any]] = None
    origin = "gateway"
    try:
        first = await hooks.run(before, {"v": PROTOCOL_VERSION, "hook": before, "args": args})
    except HookFailed as error:
        return failed(before, error, args)
    if first.response is not None:
        action = first.response["action"]
        logger.info("Source filter %s: %s", before, action)
        if action == "block":
            return blocked("source_filter_blocked", block_text(first.response), args)
        if action == "replace":
            args = dict(first.response["args"])
        elif action == "respond":
            origin = "before_hook"
            try:
                answer = {"record": await asyncio.to_thread(
                    record_for, tool, first.response["output"], requested_url=url_of(args),
                )}
            except HookFailed as error:
                return failed(before, error, args)
            if answer["record"]["status"] == "completed":
                answer["execution"] = hook_execution(tool, before)
    if answer is None:
        answer = await run(args)
    record = answer.get("record") or {}
    execution = answer.get("execution")
    attempts = record.get("attempts")
    request = {"v": PROTOCOL_VERSION, "hook": after, "args": args, "origin": origin, "output": output_of(tool, answer)}
    try:
        second = await hooks.run(after, request)
    except HookFailed as error:
        return failed(after, error, args, execution, attempts)
    if second.response is None or second.response["action"] == "pass":
        if second.response is not None:
            logger.info("Source filter %s: pass", after)
        return answer
    action = second.response["action"]
    logger.info("Source filter %s: %s", after, action)
    if action == "block":
        return blocked("source_filter_blocked", block_text(second.response), args, execution, attempts)
    try:
        replaced = await asyncio.to_thread(
            record_for, tool, second.response["output"], requested_url=url_of(args), attempts=attempts,
        )
    except HookFailed as error:
        return failed(after, error, args, execution, attempts)
    out: dict[str, Any] = {"record": replaced}
    if execution is not None:
        out["execution"] = execution
    elif replaced["status"] == "completed":
        # The script turned a failure that reached no executor into a success: it is what answered.
        out["execution"] = hook_execution(tool, after)
    return out
