"""Python owner for ``vibe computer run``."""

from __future__ import annotations

from dataclasses import dataclass
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
_SCRIPT_DIAGNOSTIC_TAIL_BYTES = 64 * 1024
_SCRIPT_RESULT_MAX_BYTES = 8 * 1024 * 1024
_SCRIPT_RUNTIME_STATE_MAX_BYTES = _SCRIPT_RESULT_MAX_BYTES
_SCRIPT_RESULT_PATH_ENV = "AVIBE_COMPUTER_RESULT_PATH"
_PROCESS_REAP_TIMEOUT_SECONDS = 0.25
_ARTIFACT_DIRECTORY_PREFIX = "avibe-computer-script-"
_SUPERVISOR_DIRECTORY_PREFIX = "avibe-computer-script-runner-"
_MAX_RETAINED_ARTIFACT_RUNS = 20
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


@dataclass(frozen=True)
class _CappedJsonRead:
    value: Any = None
    error: str | None = None


@dataclass
class _ScriptRun:
    artifact_directory: Path
    runtime_state_path: Path
    result_path: Path
    node_started: bool = False
    completed: subprocess.CompletedProcess[str] | None = None


@dataclass(frozen=True)
class _FinalizedRun:
    result: Any = None
    error: ComputerScriptRunnerError | None = None
    cause: Exception | None = None


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


def _read_capped_json(path: Path, limit: int) -> _CappedJsonRead:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return _CappedJsonRead(error="missing")
    except OSError:
        return _CappedJsonRead(error="unreadable")
    if size > limit:
        return _CappedJsonRead(error="too_large")
    try:
        with path.open("rb") as stream:
            encoded = stream.read(limit + 1)
    except OSError:
        return _CappedJsonRead(error="unreadable")
    if len(encoded) > limit:
        return _CappedJsonRead(error="too_large")
    try:
        return _CappedJsonRead(value=json.loads(encoded.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError):
        return _CappedJsonRead(error="invalid_json")


def _kill_process_group(process: subprocess.Popen[Any]) -> None:
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
    command = [str(node), str(runner)]
    supervisor_directory = Path(
        tempfile.mkdtemp(prefix=_SUPERVISOR_DIRECTORY_PREFIX)
    ).resolve()
    try:
        try:
            os.chmod(supervisor_directory, 0o700)
        except OSError:
            pass
        request_path = supervisor_directory / "request.json"
        result_path = Path(str(request["result_path"]))
        request_path.write_text(
            json.dumps(request, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            os.chmod(request_path, 0o600)
        except OSError:
            pass
        child_environment = os.environ.copy()
        child_environment[_SCRIPT_RESULT_PATH_ENV] = str(result_path)

        with request_path.open("r", encoding="utf-8") as request_input:
            process = subprocess.Popen(
                command,
                stdin=request_input,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_environment,
                start_new_session=os.name == "posix",
            )
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _kill_process_group(process)
                try:
                    process.wait(timeout=_PROCESS_REAP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                raise subprocess.TimeoutExpired(
                    command,
                    timeout_seconds,
                ) from exc

        return subprocess.CompletedProcess(
            command,
            returncode,
            "",
            "",
        )
    finally:
        shutil.rmtree(supervisor_directory, ignore_errors=True)


def _local_screenshot_path(
    error: dict[str, Any] | None,
    artifact_directory: Path,
) -> Path | None:
    if not isinstance(error, dict):
        return None
    screenshot_path = error.get("screenshot_path")
    if not isinstance(screenshot_path, str) or not screenshot_path:
        return None
    try:
        resolved = Path(screenshot_path).resolve(strict=True)
        resolved.relative_to(artifact_directory)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _retain_only_screenshot(
    artifact_directory: Path,
    screenshot_path: Path,
) -> None:
    keep_directories = set(screenshot_path.parents)
    for root, directories, files in os.walk(
        artifact_directory,
        topdown=False,
        followlinks=False,
    ):
        root_path = Path(root)
        for name in files:
            candidate = root_path / name
            try:
                is_kept = candidate.resolve(strict=True) == screenshot_path
            except OSError:
                is_kept = False
            if not is_kept:
                candidate.unlink(missing_ok=True)
        for name in directories:
            candidate = root_path / name
            if candidate in keep_directories:
                continue
            if candidate.is_symlink():
                candidate.unlink(missing_ok=True)
            else:
                shutil.rmtree(candidate, ignore_errors=True)


def _prune_retained_artifact_runs(parent: Path) -> None:
    candidates: list[tuple[float, Path]] = []
    try:
        entries = list(parent.iterdir())
    except OSError:
        return
    for candidate in entries:
        if (
            not candidate.name.startswith(_ARTIFACT_DIRECTORY_PREFIX)
            or candidate.name.startswith(_SUPERVISOR_DIRECTORY_PREFIX)
            or not candidate.is_dir()
            or candidate.is_symlink()
            or (candidate / ".active").exists()
        ):
            continue
        try:
            candidates.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _modified_at, candidate in candidates[_MAX_RETAINED_ARTIFACT_RUNS:]:
        shutil.rmtree(candidate, ignore_errors=True)


def _runtime_context(read: _CappedJsonRead) -> dict[str, Any]:
    payload = read.value if isinstance(read.value, dict) else {}
    step = payload.get("step")
    screenshot_path = payload.get("screenshot_path")
    diagnostics = payload.get("diagnostics")
    if not isinstance(step, str) or not step:
        step = "script"
    if not isinstance(screenshot_path, str) or not screenshot_path:
        screenshot_path = None
    if not isinstance(diagnostics, str):
        diagnostics = ""
    encoded_diagnostics = diagnostics.encode("utf-8", errors="replace")
    diagnostics = encoded_diagnostics[-_SCRIPT_DIAGNOSTIC_TAIL_BYTES:].decode(
        "utf-8",
        errors="replace",
    )
    return {
        "step": step,
        "screenshot_path": screenshot_path,
        "diagnostics": diagnostics,
        "read_error": read.error,
    }


def _failure_params(
    context: dict[str, Any],
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = dict(values or {})
    if context["diagnostics"]:
        params["diagnostics"] = context["diagnostics"]
    if context["read_error"] is not None:
        params["runtime_state_error"] = context["read_error"]
    return params


def _protocol_failure_envelope(
    context: dict[str, Any],
    *,
    reason: str,
    returncode: int | None,
) -> dict[str, Any]:
    if reason == "too_large":
        message = "The script result exceeded the protocol size limit."
        params = {"limit_bytes": _SCRIPT_RESULT_MAX_BYTES}
    else:
        message = "The script runtime did not produce a valid protocol result."
        params = {"reason": reason}
        if returncode is not None:
            params["status"] = returncode
    return _failure_envelope(
        name="ControllerError",
        code="computer_script_protocol_error",
        message=message,
        params=_failure_params(context, params),
        step=context["step"],
        screenshot_path=context["screenshot_path"],
        ambiguous=True,
    )


def _merge_failure_context(
    error: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(error)
    if context["step"] != "script" or not merged.get("step"):
        merged["step"] = context["step"]
    if context["screenshot_path"] is not None:
        merged["screenshot_path"] = context["screenshot_path"]
    error_params = merged.get("params")
    merged["params"] = _failure_params(
        context,
        error_params if isinstance(error_params, dict) else {},
    )
    return merged


def _finalize_run(
    run: _ScriptRun,
    pending_error: Exception | None,
) -> _FinalizedRun:
    state_read = _read_capped_json(
        run.runtime_state_path,
        _SCRIPT_RUNTIME_STATE_MAX_BYTES,
    )
    context = _runtime_context(state_read)
    failure_envelope: dict[str, Any] | None = None
    result: Any = None
    cause = pending_error

    if not run.node_started:
        finalized_error = (
            pending_error
            if isinstance(pending_error, ComputerScriptRunnerError)
            else None
        )
        if finalized_error is None and pending_error is not None:
            finalized_error = ComputerScriptRunnerError(
                "computer.cli.error.scriptRuntime",
                code="computer_script_runtime_failed",
                params={"error": str(pending_error)},
            )
    elif isinstance(pending_error, subprocess.TimeoutExpired):
        failure_envelope = _failure_envelope(
            name="Timeout",
            code="script_timeout",
            message="The script deadline expired.",
            params=_failure_params(context),
            step=context["step"],
            screenshot_path=context["screenshot_path"],
            ambiguous=True,
        )
        finalized_error = _script_failure(failure_envelope)
    elif pending_error is not None:
        failure_envelope = _failure_envelope(
            name="ControllerError",
            code="computer_script_runtime_failed",
            message="The script runtime failed.",
            params=_failure_params(context, {"error": str(pending_error)}),
            step=context["step"],
            screenshot_path=context["screenshot_path"],
            ambiguous=True,
        )
        finalized_error = _script_failure(failure_envelope)
    else:
        result_read = _read_capped_json(run.result_path, _SCRIPT_RESULT_MAX_BYTES)
        payload = result_read.value
        if result_read.error is not None:
            failure_envelope = _protocol_failure_envelope(
                context,
                reason=result_read.error,
                returncode=run.completed.returncode if run.completed else None,
            )
            finalized_error = _script_failure(failure_envelope)
        elif not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            failure_envelope = _protocol_failure_envelope(
                context,
                reason="invalid_shape",
                returncode=run.completed.returncode if run.completed else None,
            )
            finalized_error = _script_failure(failure_envelope)
        elif payload["ok"] is True:
            finalized_error = None
            result = payload.get("result")
        else:
            error = payload.get("error")
            if (
                not isinstance(error, dict)
                or set(error) != set(_failure_envelope_fields())
            ):
                failure_envelope = _protocol_failure_envelope(
                    context,
                    reason="invalid_failure_envelope",
                    returncode=run.completed.returncode if run.completed else None,
                )
            else:
                failure_envelope = _merge_failure_context(error, context)
            finalized_error = _script_failure(failure_envelope)

    retained_error = (
        failure_envelope
        if failure_envelope is not None
        else (
            finalized_error.structured_error
            if isinstance(finalized_error, ComputerScriptRunnerError)
            else None
        )
    )
    local_screenshot = _local_screenshot_path(
        retained_error,
        run.artifact_directory,
    )
    if local_screenshot is None:
        shutil.rmtree(run.artifact_directory, ignore_errors=True)
    else:
        _retain_only_screenshot(run.artifact_directory, local_screenshot)
        try:
            os.utime(run.artifact_directory)
        except OSError:
            pass
        _prune_retained_artifact_runs(run.artifact_directory.parent)

    return _FinalizedRun(
        result=result,
        error=finalized_error,
        cause=cause,
    )


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
        tempfile.mkdtemp(prefix=_ARTIFACT_DIRECTORY_PREFIX)
    ).resolve()
    try:
        os.chmod(artifact_directory, 0o700)
    except OSError:
        pass
    try:
        (artifact_directory / ".active").touch(mode=0o600, exist_ok=False)
    except OSError:
        pass

    run = _ScriptRun(
        artifact_directory=artifact_directory,
        runtime_state_path=artifact_directory / "runtime-state.json",
        result_path=artifact_directory / "result.json",
    )
    endpoint = None
    connection_token: str | None = None
    pending_error: Exception | None = None
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
        request = {
            "script_url": script_path.as_uri(),
            "sdk_url": index.as_uri(),
            "arguments": arguments,
            "endpoint": computer_script_endpoint_payload(endpoint),
            "connection_token": connection_token,
            "artifact_directory": str(artifact_directory),
            "runtime_state_path": str(run.runtime_state_path),
            "result_path": str(run.result_path),
        }
        run.node_started = True
        run.completed = _run_node_process(
            node,
            runner,
            request,
            remaining_seconds,
        )
    except Exception as exc:
        pending_error = exc
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

        finalized = _finalize_run(run, pending_error)

    if finalized.error is not None:
        if finalized.cause is not None and finalized.cause is not finalized.error:
            raise finalized.error from finalized.cause
        raise finalized.error
    return finalized.result
