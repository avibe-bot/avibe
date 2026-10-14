"""Python owner for ``vibe computer run``."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile
import time
from typing import Any, Iterable

from vibe.desktop_runtime import private_desktop_node_bin
from vibe.internal_client import (
    InternalServerTimeout,
    InternalServerUnavailable,
    computer_script_close_sync,
    computer_script_endpoint_payload,
    computer_script_open_sync,
    resolve_computer_script_endpoint,
)


DEFAULT_SCRIPT_TIMEOUT_SECONDS = 300.0
_FAILURE_ENVELOPE_FIELDS: tuple[str, ...] | None = None

_SCRIPT_OPEN_ERROR_KEYS = {
    "active_turn_required": "computer.cli.error.missingCallerTurn",
    "session_archived": "computer.cli.error.sessionArchived",
}


class ComputerScriptRunnerError(RuntimeError):
    """A script could not be prepared or did not complete successfully."""

    def __init__(
        self,
        key: str,
        *,
        code: str,
        params: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
        structured_error: dict[str, Any] | None = None,
    ) -> None:
        self.key = key
        self.code = code
        self.params = params or {}
        self.details = details or {}
        self.structured_error = structured_error
        super().__init__(key)


def parse_script_arguments(values: Iterable[str]) -> dict[str, str]:
    """Parse repeatable ``NAME=VALUE`` arguments without guessing value types."""

    parsed: dict[str, str] = {}
    for raw in values:
        name, separator, value = raw.partition("=")
        name = name.strip()
        if separator != "=" or not name:
            raise ComputerScriptRunnerError(
                "computer.cli.error.scriptArgNameValue",
                code="invalid_computer_script_argument",
            )
        if name in parsed:
            raise ComputerScriptRunnerError(
                "computer.cli.error.scriptArgDuplicate",
                code="duplicate_computer_script_argument",
                params={"name": name},
            )
        if value.startswith("@"):
            source = Path(value[1:]).expanduser()
            try:
                value = source.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ComputerScriptRunnerError(
                    "computer.cli.error.scriptArgFile",
                    code="computer_script_argument_file_unreadable",
                    params={"path": str(source), "error": str(exc)},
                ) from exc
        parsed[name] = value
    return parsed


def _script_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ComputerScriptRunnerError(
            "computer.cli.error.scriptFile",
            code="computer_script_file_unreadable",
            params={"path": str(path), "error": str(exc)},
        ) from exc
    if not resolved.is_file() or resolved.suffix.lower() != ".mjs":
        raise ComputerScriptRunnerError(
            "computer.cli.error.scriptFileType",
            code="computer_script_file_unreadable",
            params={"path": str(path)},
        )
    return resolved


def _sdk_directory() -> Path:
    return Path(__file__).resolve().parent / "computer_sdk"


def _failure_envelope_fields() -> tuple[str, ...]:
    global _FAILURE_ENVELOPE_FIELDS
    if _FAILURE_ENVELOPE_FIELDS is not None:
        return _FAILURE_ENVELOPE_FIELDS
    contract_path = _sdk_directory() / "failure-envelope.json"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        fields = contract["fields"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ComputerScriptRunnerError(
            "computer.cli.error.scriptSdkUnavailable",
            code="computer_script_unavailable",
        ) from exc
    if (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(field, str) or not field for field in fields)
        or len(set(fields)) != len(fields)
    ):
        raise ComputerScriptRunnerError(
            "computer.cli.error.scriptSdkUnavailable",
            code="computer_script_unavailable",
        )
    _FAILURE_ENVELOPE_FIELDS = tuple(fields)
    return _FAILURE_ENVELOPE_FIELDS


def _failure_envelope(**values: Any) -> dict[str, Any]:
    fields = _failure_envelope_fields()
    if set(values) != set(fields):
        raise RuntimeError("failure envelope implementation does not match its contract")
    return {field: values[field] for field in fields}


def _runtime_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _run_node_process(
    node: Path,
    runner: Path,
    request: dict[str, Any],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        [str(node), str(runner)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name == "posix",
    )
    command = [str(node), str(runner)]
    try:
        stdout, stderr = process.communicate(
            input=json.dumps(request, ensure_ascii=False),
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output=stdout,
            stderr=stderr,
        ) from exc
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout,
        stderr,
    )


def _decode_runner_output(
    stdout: str,
    stderr: str,
    returncode: int,
) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        detail = (stderr or stdout).strip()
        if not detail:
            raise ComputerScriptRunnerError(
                "computer.cli.error.scriptRuntimeExit",
                code="computer_script_runtime_failed",
                params={"status": returncode},
            ) from exc
        raise ComputerScriptRunnerError(
            "computer.cli.error.scriptRuntime",
            code="computer_script_runtime_failed",
            params={"error": detail[:2048]},
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise ComputerScriptRunnerError(
            "computer.cli.error.scriptRuntimeInvalid",
            code="computer_script_runtime_failed",
        )
    return payload


def _keeps_local_screenshot(
    payload: dict[str, Any] | None,
    artifact_directory: Path,
) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return False
    screenshot_path = payload["error"].get("screenshot_path")
    if not isinstance(screenshot_path, str) or not screenshot_path:
        return False
    try:
        resolved = Path(screenshot_path).resolve(strict=True)
        resolved.relative_to(artifact_directory)
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved.is_file()


def _display_seconds(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _script_failure(error: dict[str, Any]) -> ComputerScriptRunnerError:
    if set(error) != set(_failure_envelope_fields()):
        return ComputerScriptRunnerError(
            "computer.cli.error.scriptRuntimeInvalid",
            code="computer_script_runtime_failed",
        )
    step = str(error.get("step") or "script")
    message = str(error.get("message") or "the script failed")
    code = str(error.get("code") or "computer_script_failed")
    failure_envelope = dict(error)
    return ComputerScriptRunnerError(
        "computer.cli.error.scriptFailed",
        code=code,
        params={"step": step, "error": message},
        details=failure_envelope,
        structured_error=failure_envelope,
    )


def run_computer_script(
    *,
    caller_session_id: str,
    script_file: str,
    arg_values: Iterable[str],
    timeout_seconds: float = DEFAULT_SCRIPT_TIMEOUT_SECONDS,
) -> Any:
    """Run one ESM script with an exact-Turn-bound Controller connection."""

    node = private_desktop_node_bin()
    if node is None:
        raise ComputerScriptRunnerError(
            "computer.cli.error.scriptNodeUnavailable",
            code="computer_script_unavailable",
        )
    script_path = _script_path(script_file)
    arguments = parse_script_arguments(arg_values)

    sdk_directory = _sdk_directory()
    runner = sdk_directory / "runner.mjs"
    index = sdk_directory / "index.mjs"
    if not runner.is_file() or not index.is_file():
        raise ComputerScriptRunnerError(
            "computer.cli.error.scriptSdkUnavailable",
            code="computer_script_unavailable",
        )

    deadline = time.monotonic() + timeout_seconds
    display_seconds = _display_seconds(timeout_seconds)
    artifact_directory = Path(
        tempfile.mkdtemp(prefix="avibe-computer-script-")
    ).resolve()
    try:
        os.chmod(artifact_directory, 0o700)
    except OSError:
        pass

    payload: dict[str, Any] | None = None
    endpoint = None
    connection_token: str | None = None
    try:
        try:
            endpoint = resolve_computer_script_endpoint()
            open_timeout = deadline - time.monotonic()
            if open_timeout <= 0:
                raise ComputerScriptRunnerError(
                    "computer.cli.error.scriptTimedOut",
                    code="script_timeout",
                    params={"seconds": display_seconds},
                    details={"step": "script"},
                )
            opened = computer_script_open_sync(
                endpoint=endpoint,
                caller_session_id=caller_session_id,
                timeout_seconds=timeout_seconds,
                request_timeout=min(10.0, open_timeout),
            )
        except InternalServerUnavailable as exc:
            raise ComputerScriptRunnerError(
                "computer.cli.error.serviceUnavailable",
                code="controller_unavailable",
                params={"error": str(exc)},
            ) from exc
        except InternalServerTimeout as exc:
            if time.monotonic() >= deadline:
                raise ComputerScriptRunnerError(
                    "computer.cli.error.scriptTimedOut",
                    code="script_timeout",
                    params={"seconds": display_seconds},
                    details={"step": "script"},
                ) from exc
            raise ComputerScriptRunnerError(
                "computer.cli.error.requestTimedOut",
                code="computer_script_open_timeout",
                params={"error": str(exc)},
            ) from exc

        body = opened.get("body")
        if not isinstance(body, dict):
            raise ComputerScriptRunnerError(
                "computer.cli.error.invalidResponse",
                code="invalid_computer_script_response",
            )
        candidate_token = body.get("connection_token")
        if (
            body.get("ok") is not True
            or not isinstance(candidate_token, str)
            or not candidate_token
        ):
            code = str(body.get("error") or "computer_script_open_rejected")
            key = _SCRIPT_OPEN_ERROR_KEYS.get(
                code,
                "computer.cli.error.scriptOpenRejected",
            )
            raise ComputerScriptRunnerError(
                key,
                code=code,
                params={} if code in _SCRIPT_OPEN_ERROR_KEYS else {"code": code},
            )
        connection_token = candidate_token

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise ComputerScriptRunnerError(
                "computer.cli.error.scriptTimedOut",
                code="script_timeout",
                params={"seconds": display_seconds},
                details={"step": "script"},
            )
        runtime_state_path = artifact_directory / "runtime-state.json"
        request = {
            "script_url": script_path.as_uri(),
            "sdk_url": index.as_uri(),
            "arguments": arguments,
            "endpoint": computer_script_endpoint_payload(endpoint),
            "connection_token": connection_token,
            "artifact_directory": str(artifact_directory),
            "runtime_state_path": str(runtime_state_path),
        }
        try:
            completed = _run_node_process(
                node,
                runner,
                request,
                remaining_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            runtime_state = _runtime_state(runtime_state_path)
            screenshot_path = runtime_state.get("screenshot_path")
            if not isinstance(screenshot_path, str) or not screenshot_path:
                screenshot_path = None
            failure_envelope = _failure_envelope(
                name="Timeout",
                code="script_timeout",
                message="The script deadline expired.",
                params={},
                step=str(runtime_state.get("step") or "script"),
                screenshot_path=screenshot_path,
                ambiguous=True,
            )
            payload = {"ok": False, "error": failure_envelope}
            raise _script_failure(failure_envelope) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ComputerScriptRunnerError(
                "computer.cli.error.scriptRuntime",
                code="computer_script_runtime_failed",
                params={"error": str(exc)},
            ) from exc

        payload = _decode_runner_output(
            completed.stdout,
            completed.stderr,
            completed.returncode,
        )
        if payload["ok"] is True:
            return payload.get("result")

        error = payload.get("error")
        if not isinstance(error, dict):
            error = {}
        raise _script_failure(error)
    finally:
        close_timeout = deadline - time.monotonic()
        if (
            endpoint is not None
            and connection_token is not None
            and close_timeout > 0
        ):
            try:
                computer_script_close_sync(
                    endpoint=endpoint,
                    connection_token=connection_token,
                    timeout=min(1.0, close_timeout),
                )
            except (InternalServerUnavailable, InternalServerTimeout):
                pass

        if not _keeps_local_screenshot(payload, artifact_directory):
            shutil.rmtree(artifact_directory, ignore_errors=True)
