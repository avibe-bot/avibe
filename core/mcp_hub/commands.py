"""Tool schema -> declarative CLI flags (``commands.json``), argv parsing and validation.

``commands.json`` is data interpreted by the shared ``vibe mcp`` runner: flags map
exact property keys, nothing is aliased, case-converted or defaulted.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from core.mcp_hub.errors import McpHubError

_FLAG_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_RESERVED_KEYS = {"args-json", "args-file", "args-stdin", "help"}
_FLAG_KINDS = {"string", "number", "integer", "boolean", "array", "object"}
INPUT_MODES = ("--args-json", "--args-file", "--args-stdin")


def _validator_for(schema: Any):
    """A JSON Schema validator with remote ``$ref`` retrieval disabled, or ``None``."""

    import jsonschema
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    def _no_remote(uri: str):
        raise NoSuchResource(ref=uri)

    if not isinstance(schema, dict):
        return None
    try:
        cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
        cls.check_schema(schema)
        from referencing.jsonschema import DRAFT202012

        registry = Registry(retrieve=_no_remote).with_resource("", DRAFT202012.create_resource(schema))
        validator = cls(schema, registry=registry)
        validator.is_valid({})
        _assert_refs_resolvable(schema, registry.resolver())
    except Exception:
        return None
    return validator


def _assert_refs_resolvable(node: Any, resolver) -> None:
    """Walk nested ``$ref`` so an unresolvable optional property is not published as validated."""

    if isinstance(node, dict):
        if "$ref" in node:
            resolver.lookup(node["$ref"])
        for value in node.values():
            _assert_refs_resolvable(value, resolver)
    elif isinstance(node, list):
        for item in node:
            _assert_refs_resolvable(item, resolver)


def build_command(tool: dict[str, Any]) -> dict[str, Any]:
    """The ``commands.json`` entry for one tool descriptor (``tools/list`` shape)."""

    schema = tool.get("inputSchema")
    validated = _validator_for(schema) is not None
    properties = schema.get("properties") if validated and isinstance(schema, dict) else None
    required = set(schema.get("required") or []) if validated and isinstance(schema, dict) else set()
    flags: list[dict[str, Any]] = []
    json_only_keys: list[str] = []
    if isinstance(properties, dict):
        for key in sorted(properties):
            prop = properties[key] if isinstance(properties[key], dict) else {}
            kind = prop.get("type")
            if (
                isinstance(kind, str)
                and kind in _FLAG_KINDS
                and _FLAG_KEY.match(key)
                and key not in _RESERVED_KEYS
                and not any(word in prop for word in ("anyOf", "oneOf", "allOf", "$ref"))
            ):
                flags.append({
                    "flag": f"--{key}",
                    "key": key,
                    "kind": kind,
                    "required": key in required,
                    "description": _one_line(prop.get("description")),
                })
            else:
                json_only_keys.append(key)
    return {
        "name": tool["name"],
        "description": _one_line(tool.get("description"), limit=None),
        "validated": validated,
        "flags": flags,
        "jsonOnlyKeys": json_only_keys,
        "requiredJsonOnly": sorted(required & set(json_only_keys)),
    }


def _one_line(value: object, *, limit: Optional[int] = 200) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if limit is None else text[:limit]


def _usage(message: str, hint: Optional[str] = None) -> McpHubError:
    return McpHubError("usage", message, hint=hint, usage=True)


def _flag_value(flag: dict[str, Any], raw: str) -> Any:
    kind = flag["kind"]
    if kind == "string":
        return raw
    if kind == "boolean":
        if raw not in {"true", "false"}:
            raise _usage(f"{flag['flag']} expects true or false.")
        return raw == "true"
    try:
        value = json.loads(raw)
    except ValueError:
        raise _usage(f"{flag['flag']} expects a JSON {kind}.") from None
    ok = {
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }[kind]
    if not ok:
        raise _usage(f"{flag['flag']} expects a JSON {kind}.")
    return value


def parse_tool_argv(
    command: dict[str, Any],
    argv: list[str],
    *,
    read_file=None,
    read_stdin=None,
) -> dict[str, Any]:
    """Turn ``vibe mcp <server> <tool>`` arguments into the tool's argument object."""

    flags = {flag["flag"]: flag for flag in command.get("flags") or []}
    arguments: dict[str, Any] = {}
    mode: Optional[str] = None
    payload: Any = None
    index = 0
    while index < len(argv):
        token = argv[index]
        option, eq, inline = token.partition("=")
        if not token.startswith("--"):
            raise _usage(f"Unexpected argument {token!r}.")
        if option in INPUT_MODES:
            if mode is not None or arguments:
                raise _usage("Use one input mode per call: flags, --args-json, --args-file or --args-stdin.")
            mode = option
            if option == "--args-stdin":
                if eq:
                    raise _usage("--args-stdin takes no value.")
                payload = read_stdin() if read_stdin else ""
            else:
                if not eq:
                    if index + 1 >= len(argv):
                        raise _usage(f"{option} requires a value.")
                    index += 1
                    inline = argv[index]
                payload = inline if option == "--args-json" else (read_file(inline) if read_file else "")
            index += 1
            continue
        flag = flags.get(option)
        if flag is None:
            raise _usage(
                f"Unknown flag {option}.",
                hint="Inspect arguments with --help; keys without a flag need --args-json.",
            )
        if mode is not None:
            raise _usage("Use one input mode per call: flags, --args-json, --args-file or --args-stdin.")
        if flag["key"] in arguments:
            raise _usage(f"{option} was given more than once.")
        if not eq:
            if index + 1 >= len(argv):
                raise _usage(f"{option} requires a value.")
            index += 1
            inline = argv[index]
        arguments[flag["key"]] = _flag_value(flag, inline)
        index += 1
    if mode is None:
        return arguments
    try:
        value = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise _usage(f"{mode} is not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise _usage(f"{mode} must be a JSON object.")
    return value


def validate_arguments(tool: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Validate against the published input schema; unhandleable schemas are not validated."""

    validator = _validator_for(tool.get("inputSchema"))
    if validator is None:
        return
    errors = sorted(validator.iter_errors(arguments), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        where = "/".join(str(part) for part in first.path) or "(root)"
        raise McpHubError(
            "invalid_arguments",
            f"Arguments do not match the tool schema at {where}: {first.message}",
            usage=True,
        )


def render_tool_help(server: str, command: dict[str, Any], escape) -> str:
    lines = [f"vibe mcp {server} {_selector(command['name'])} [flags]"]
    if command.get("description"):
        lines += ["", escape(command["description"])]
    flags = command.get("flags") or []
    if flags:
        lines += ["", "Flags:"]
        for flag in flags:
            required = " (required)" if flag["required"] else ""
            detail = f"  {escape(flag['description'])}" if flag["description"] else ""
            lines.append(f"  {escape(flag['flag'])} <{flag['kind']}>{required}{detail}")
    if command.get("jsonOnlyKeys"):
        keys = ", ".join(escape(key) for key in command["jsonOnlyKeys"])
        lines += ["", f"Keys only accepted via --args-json/--args-file/--args-stdin: {keys}"]
    if command.get("requiredJsonOnly"):
        lines.append("A required key has no flag: call this tool with --args-json.")
    if not command.get("validated"):
        lines += ["", "This tool's input schema could not be validated locally; pass arguments as JSON."]
    lines += [
        "",
        "Input: flags, or exactly one of --args-json '<object>', --args-file <path>, --args-stdin.",
        f"Schema: vibe mcp {server} --list --format json",
    ]
    return "\n".join(lines)


def _selector(tool_name: str) -> str:
    return f"-- {tool_name}" if tool_name.startswith("-") else tool_name
