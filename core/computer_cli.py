"""CLI contract and rendering helpers for Controller-owned Computer Use."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any, Callable, Mapping


COMPUTER_CLI_TOOLS: dict[tuple[str, ...], str] = {
    ("observe", "apps"): "list_apps",
    ("observe", "windows"): "list_windows",
    ("observe", "window"): "get_window_state",
    ("zoom",): "zoom",
    ("activate",): "bring_to_front",
    ("click",): "click",
    ("type",): "type_text",
    ("key",): "press_key",
    ("hotkey",): "hotkey",
    ("scroll",): "scroll",
    ("drag",): "drag",
    ("set-value",): "set_value",
    ("clipboard", "read"): "clipboard_read",
    ("clipboard", "write"): "clipboard_write",
    ("launch",): "launch_app",
    ("force-quit",): "kill_app",
}

OBSERVATION_TOOLS = frozenset(
    {
        "list_apps",
        "list_windows",
        "get_window_state",
        "zoom",
    }
)
SCREENSHOT_TOOLS = frozenset({"get_window_state", "zoom"})


# Upper bound on how long one ``vibe computer js`` call can stay admitted: the
# Controller leases its exact-Turn connection for this long, and every path
# (queue cap, per-call timeout, cancel grace, kernel start) settles inside it.
# The CLI waits at least this long so it never gives up before the Controller.
COMPUTER_JS_CONNECTION_LEASE_SECONDS = 3600.0
COMPUTER_JS_CLIENT_MARGIN_SECONDS = 60.0


class ComputerCliError(ValueError):
    """The CLI request cannot be translated into a driver call."""

    def __init__(self, key: str, **params: Any) -> None:
        self.key = key
        self.params = params
        super().__init__(key)


def parse_arguments_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    source = raw
    if raw.startswith("@"):
        try:
            source = Path(raw[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise ComputerCliError(
                "computer.cli.error.cannotReadArgsJson",
                error=str(exc),
            ) from exc
    try:
        payload = json.loads(source)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ComputerCliError("computer.cli.error.argsJsonObject") from exc
    if not isinstance(payload, dict):
        raise ComputerCliError("computer.cli.error.argsJsonObject")
    return dict(payload)


def parse_argument_overrides(values: list[str] | None) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for raw in values or []:
        name, separator, value = raw.partition("=")
        if not separator or not name.strip():
            raise ComputerCliError("computer.cli.error.argNameValue")
        name = name.strip()
        try:
            parsed: Any = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = value
        overrides[name] = parsed
    return overrides


def build_driver_arguments(
    args_json: str | None,
    overrides: list[str] | None,
) -> dict[str, Any]:
    arguments = parse_arguments_json(args_json)
    for key, value in parse_argument_overrides(overrides).items():
        arguments[key] = value
    if "session" in arguments:
        raise ComputerCliError("computer.cli.error.sessionArgumentForbidden")
    return arguments


def prepare_arguments_for_output(
    tool: str,
    arguments: dict[str, Any],
    output_path: str | None,
) -> dict[str, Any]:
    if not output_path:
        return arguments
    if tool not in SCREENSHOT_TOOLS:
        return arguments
    if tool == "zoom":
        return arguments
    if "screenshot_out_file" in arguments:
        raise ComputerCliError("computer.cli.error.outputArgumentConflict")
    prepared = dict(arguments)
    prepared["screenshot_out_file"] = str(Path(output_path).expanduser().resolve())
    return prepared


def tool_for_command(path: tuple[str, ...]) -> str:
    try:
        return COMPUTER_CLI_TOOLS[path]
    except KeyError as exc:
        raise ComputerCliError(
            "computer.cli.error.unsupportedCommand",
            command=" ".join(path),
        ) from exc


def _structured_payloads(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return [structured]

    payloads: list[dict[str, Any]] = []
    content = result.get("content")
    if not isinstance(content, list):
        return payloads
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        if text.startswith("Structured content:\n"):
            text = text.split("\n", 1)[1]
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _error_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    for payload in _structured_payloads(result):
        if "code" in payload:
            return payload
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return {"code": "computer_use_error", "params": {}}
    return {"code": "computer_use_error", "params": {}}


_COMPUTER_ERROR_I18N_KEYS = {
    "active_turn_required": "computer.cli.error.activeTurnRequired",
    "ambiguous": "computer.cli.error.scriptAmbiguous",
    "application_not_found": "computer.cli.error.scriptTargetNotFound",
    "computer_script_request_failed": "computer.cli.error.scriptControllerRejected",
    "computer_script_request_too_large": "computer.cli.error.scriptRequestTooLarge",
    "computer_script_message_too_large": "computer.cli.error.scriptMessageTooLarge",
    "computer_script_notify_failed": "computer.cli.error.scriptNotifyFailed",
    "invalid_computer_script_message": "computer.cli.error.scriptInvalidMessage",
    "computer_script_descriptor_required": "computer.cli.error.scriptInvalidDescriptor",
    "computer_script_protocol_error": "computer.cli.error.scriptRuntimeInvalid",
    "computer_use_unavailable": "computer.cli.error.computerUseUnavailable",
    "computer_use_error": "computer.cli.error.genericDriverError",
    "controller_timeout": "computer.cli.error.scriptOperationTimedOut",
    "controller_unavailable": "computer.cli.error.scriptControllerUnavailable",
    "confirm_refused": "computer.cli.error.confirmRefused",
    "confirm_already_pending": "computer.cli.error.confirmAlreadyPending",
    "confirm_approver_unavailable": "computer.cli.error.confirmApproverUnavailable",
    "confirm_prompt_failed": "computer.cli.error.confirmPromptFailed",
    "confirm_unattended": "computer.cli.error.confirmUnattended",
    "desktop_busy": "computer.cli.error.targetBusy",
    "element_not_actionable": "computer.cli.error.scriptElementNotActionable",
    "element_not_found": "computer.cli.error.scriptTargetNotFound",
    "invalid_arguments": "computer.cli.error.invalidDriverArguments",
    "invalid_computer_use_request": "computer.cli.error.requestRejected",
    "invalid_computer_script_interaction": "computer.cli.error.requestRejected",
    "invalid_computer_use_response": "computer.cli.error.scriptInvalidControllerResponse",
    "invalid_computer_script_descriptor": "computer.cli.error.scriptInvalidDescriptor",
    "invalid_selector": "computer.cli.error.scriptInvalidSelector",
    "invalid_timeout": "computer.cli.error.scriptInvalidTimeout",
    "invalid_computer_js_code": "computer.cli.error.jsCodeInvalid",
    "invalid_computer_js_request": "computer.cli.error.requestRejected",
    "computer_js_code_too_large": "computer.cli.error.jsCodeTooLarge",
    "js_call_inactive": "computer.cli.error.jsCallInactive",
    "js_kernel_error": "computer.cli.error.jsKernelError",
    "js_kernel_busy": "computer.cli.error.jsKernelBusy",
    "js_kernel_reset": "computer.cli.error.jsKernelReset",
    "js_kernel_unavailable": "computer.cli.error.jsKernelUnavailable",
    "js_timeout": "computer.cli.error.jsTimeout",
    "not_found": "computer.cli.error.scriptTargetNotFound",
    "observe_first": "computer.cli.error.observeFirst",
    "platform_unsupported": "computer.cli.error.platformUnsupported",
    "script_connection_invalid": "computer.cli.error.scriptConnectionInvalid",
    "script_timeout": "computer.cli.error.scriptDeadlineExpired",
    "script_turn_ended": "computer.cli.error.scriptTurnEnded",
    "session_archived": "computer.cli.error.sessionArchived",
    "session_argument_forbidden": "computer.cli.error.sessionArgumentForbidden",
    "session_required": "computer.cli.error.missingCallerSession",
    "session_unavailable": "computer.cli.error.sessionUnavailable",
    "snapshot_not_found": "computer.cli.error.scriptTargetNotFound",
    "stale_app_handle": "computer.cli.error.scriptStaleAppHandle",
    "stale_window_handle": "computer.cli.error.scriptStaleWindowHandle",
    "timeout": "computer.cli.error.scriptOperationTimedOut",
    "target_busy": "computer.cli.error.targetBusy",
    "unknown_tool": "computer.cli.error.unknownTool",
    "upstream_error": "computer.cli.error.upstreamError",
    "upstream_protocol_error": "computer.cli.error.upstreamProtocolError",
    "upstream_tool_error": "computer.cli.error.upstreamToolError",
    "upstream_unavailable": "computer.cli.error.upstreamUnavailable",
    "window_id_not_found": "computer.cli.error.scriptTargetNotFound",
    "window_not_found": "computer.cli.error.scriptTargetNotFound",
    "window_order_unavailable": "computer.cli.error.scriptWindowOrderUnavailable",
    "window_target_required": "computer.cli.error.windowTargetRequired",
}

_COMPUTER_STATUS_I18N_KEYS = {
    "off": "computer.cli.status.off",
    "needs_permission": "computer.cli.status.needsPermission",
    "needs_runtime": "computer.cli.status.needsRuntime",
    "starting": "computer.cli.status.starting",
    "error": "computer.cli.status.error",
    "stopped": "computer.cli.status.stopped",
    "unavailable": "computer.cli.status.unavailable",
}

_COMPUTER_REASON_I18N_KEYS = {
    "never_enabled": "computer.cli.reason.neverEnabled",
    "toggle_off": "computer.cli.reason.toggleOff",
    "accessibility": "computer.cli.reason.accessibility",
    "screen_recording": "computer.cli.reason.screenRecording",
    "runtime_unavailable": "computer.cli.reason.runtimeUnavailable",
    "runtime_too_old": "computer.cli.reason.runtimeTooOld",
    "state_unwritable": "computer.cli.reason.stateUnwritable",
    "assets_invalid": "computer.cli.reason.assetsInvalid",
    "spawn_failed": "computer.cli.reason.spawnFailed",
    "daemon_exited": "computer.cli.reason.daemonExited",
    "health_timeout": "computer.cli.reason.healthTimeout",
    "bundle_identity": "computer.cli.reason.bundleIdentity",
    "ax_capability": "computer.cli.reason.axCapability",
    "capture_failed": "computer.cli.reason.captureFailed",
    "driver_health_failed": "computer.cli.reason.driverHealthFailed",
    "endpoint_busy": "computer.cli.reason.endpointBusy",
    "endpoint_unremovable": "computer.cli.reason.endpointUnremovable",
    "socket_unreachable": "computer.cli.reason.socketUnreachable",
    "socket_path_too_long": "computer.cli.reason.socketPathTooLong",
    "invalid_state_file": "computer.cli.reason.invalidStateFile",
    "snapshot_invalid": "computer.cli.reason.snapshotInvalid",
    "shell_not_running": "computer.cli.reason.shellNotRunning",
    "daemon_unreachable": "computer.cli.reason.daemonUnreachable",
    "daemon_generation_changed": "computer.cli.reason.daemonGenerationChanged",
}


def translate_computer_error(
    error: Mapping[str, Any],
    *,
    translate: Callable[..., str],
    fallback: str | None = None,
) -> str:
    """Render a Controller or driver error without trusting prose from it."""

    code = str(error.get("code") or "computer_use_error")
    key = _COMPUTER_ERROR_I18N_KEYS.get(code)
    if key is None:
        if fallback is not None:
            return fallback
        key = "computer.cli.error.genericDriverError"
    params = error.get("params")
    if code == "unknown_tool" and isinstance(params, Mapping):
        return translate(key, tool=str(params.get("tool") or ""))
    if code == "computer_use_unavailable" and isinstance(params, Mapping):
        status_code = str(params.get("status") or "unknown")
        reason_value = params.get("reason")
        reason_code = (
            str(reason_value)
            if isinstance(reason_value, str) and reason_value
            else "none"
        )
        status_key = _COMPUTER_STATUS_I18N_KEYS.get(
            status_code,
            "computer.cli.status.unknown",
        )
        reason_key = _COMPUTER_REASON_I18N_KEYS.get(
            reason_code,
            (
                "computer.cli.reason.none"
                if reason_code == "none"
                else "computer.cli.reason.unknown"
            ),
        )
        return translate(
            key,
            statusCode=status_code,
            status=translate(status_key, code=status_code),
            reasonCode=reason_code,
            reason=translate(reason_key, code=reason_code),
        )
    return translate(key)


def _decode_image_content(result: Mapping[str, Any]) -> bytes | None:
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        data = item.get("data")
        if not isinstance(data, str):
            continue
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None
    return None


def write_screenshot(
    tool: str,
    result: Mapping[str, Any],
    output_path: str | None,
) -> str | None:
    if not output_path:
        return None
    target = Path(output_path).expanduser()
    if tool == "get_window_state":
        payloads = _structured_payloads(result)
        for payload in payloads:
            path = payload.get("screenshot_file_path")
            if isinstance(path, str) and path:
                return str(Path(path).expanduser().resolve())
    image = _decode_image_content(result)
    if image is None:
        raise ComputerCliError("computer.cli.error.screenshotMissing", tool=tool)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(image)
    return str(target.resolve())


def _render_accessibility(
    payload: Mapping[str, Any],
    translate: Callable[..., str],
) -> list[str]:
    lines: list[str] = []
    if "elements" not in payload and "tree_markdown" not in payload:
        return lines
    degraded = payload.get("degraded_reason")
    complete = payload.get("elements_complete")
    state_key = "available"
    if degraded:
        state_key = "degraded"
    elif complete is False or payload.get("truncated") is True:
        state_key = "partial"
    lines.append(
        translate(
            "computer.cli.result.accessibility",
            state=translate(
                f"computer.cli.result.accessibilityState.{state_key}",
                reason=degraded,
            ),
        )
    )
    elements = payload.get("elements")
    if isinstance(elements, list):
        lines.append(translate("computer.cli.result.elementTokens"))
        for element in elements:
            if not isinstance(element, dict):
                continue
            token = str(element.get("element_token") or "").strip()
            if not token:
                continue
            role = str(element.get("role") or "element")
            label = str(element.get("label") or element.get("value") or "").strip()
            suffix = f" {label}" if label else ""
            lines.append(f"  {token} [{role}]{suffix}")
    tree = payload.get("tree_markdown")
    if isinstance(tree, str) and tree.strip():
        lines.append(tree.rstrip())
    return lines


def format_human_result(
    tool: str,
    result: Mapping[str, Any],
    *,
    output_path: str | None = None,
    translate: Callable[..., str],
) -> str:
    if result.get("isError") is True:
        error = _error_payload(result)
        code = str(error.get("code") or "computer_use_error")
        message = translate_computer_error(error, translate=translate)
        return translate(
            "computer.cli.result.error",
            code=code,
            message=message,
        )

    lines = [translate("computer.cli.result.succeeded", tool=tool)]
    rendered = False
    for payload in _structured_payloads(result):
        accessibility = _render_accessibility(payload, translate)
        if accessibility:
            lines.extend(accessibility)
            rendered = True
            continue
        for key in ("apps", "windows"):
            value = payload.get(key)
            if isinstance(value, list):
                lines.append(translate(f"computer.cli.result.{key}"))
                lines.extend(f"  {json.dumps(item, ensure_ascii=False, sort_keys=True)}" for item in value)
                rendered = True
        if not rendered:
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            rendered = True
    if output_path:
        lines.append(translate("computer.cli.result.screenshot", path=output_path))
    if not rendered:
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and not text.startswith("Structured content:"):
                        lines.append(text)
    return "\n".join(lines)
