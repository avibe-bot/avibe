"""CLI contract and rendering helpers for Controller-owned Computer Use."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any, Mapping


COMPUTER_CLI_TOOLS: dict[tuple[str, ...], str] = {
    ("observe", "apps"): "list_apps",
    ("observe", "windows"): "list_windows",
    ("observe", "window"): "get_window_state",
    ("zoom",): "zoom",
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
    ("quit",): "kill_app",
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


class ComputerCliError(ValueError):
    """The CLI request cannot be translated into a driver call."""


def parse_arguments_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    source = raw
    if raw.startswith("@"):
        try:
            source = Path(raw[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise ComputerCliError(f"cannot read --args-json file: {exc}") from exc
    try:
        payload = json.loads(source)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ComputerCliError("--args-json must contain a JSON object") from exc
    if not isinstance(payload, dict):
        raise ComputerCliError("--args-json must contain a JSON object")
    return dict(payload)


def parse_argument_overrides(values: list[str] | None) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for raw in values or []:
        name, separator, value = raw.partition("=")
        if not separator or not name.strip():
            raise ComputerCliError("--arg values must use name=value")
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
        raise ComputerCliError(
            "session is bound to the current Avibe Turn and cannot be supplied "
            "through --args-json or --arg"
        )
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
        raise ComputerCliError(
            "use --output or screenshot_out_file in --args-json, not both"
        )
    prepared = dict(arguments)
    prepared["screenshot_out_file"] = str(Path(output_path).expanduser())
    return prepared


def tool_for_command(path: tuple[str, ...]) -> str:
    try:
        return COMPUTER_CLI_TOOLS[path]
    except KeyError as exc:
        raise ComputerCliError(
            f"unsupported Computer Use command: {' '.join(path)}"
        ) from exc


def _structured_payloads(result: Mapping[str, Any]) -> list[dict[str, Any]]:
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
        if "code" in payload or "message" in payload:
            return payload
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return {"message": item["text"]}
    return {"message": "computer use failed"}


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
        raise ComputerCliError(
            f"{tool} did not return an image that could be written to --output"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(image)
    return str(target.resolve())


def _render_accessibility(payload: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    if "elements" not in payload and "tree_markdown" not in payload:
        return lines
    degraded = payload.get("degraded_reason")
    complete = payload.get("elements_complete")
    state = "available"
    if degraded:
        state = f"degraded ({degraded})"
    elif complete is False or payload.get("truncated") is True:
        state = "partial"
    lines.append(f"Accessibility: {state}")
    elements = payload.get("elements")
    if isinstance(elements, list):
        lines.append("Element tokens:")
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
) -> str:
    if result.get("isError") is True:
        error = _error_payload(result)
        code = str(error.get("code") or "computer_use_error")
        message = str(error.get("message") or "computer use failed")
        return f"Computer Use error [{code}]: {message}"

    lines = [f"Computer Use: {tool} succeeded."]
    rendered = False
    for payload in _structured_payloads(result):
        accessibility = _render_accessibility(payload)
        if accessibility:
            lines.extend(accessibility)
            rendered = True
            continue
        for key in ("apps", "windows"):
            value = payload.get(key)
            if isinstance(value, list):
                lines.append(f"{key.title()}:")
                lines.extend(f"  {json.dumps(item, ensure_ascii=False, sort_keys=True)}" for item in value)
                rendered = True
        if not rendered:
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            rendered = True
    if output_path:
        lines.append(f"Screenshot: {output_path}")
    if not rendered:
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and not text.startswith("Structured content:"):
                        lines.append(text)
    return "\n".join(lines)
