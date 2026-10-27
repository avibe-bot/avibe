"""``vibe mcp``: static discovery from generated packages, calls and management via the owner.

List and help read local artifacts only, so they work offline and with the owner
down. Calls and management go to the Controller over control IPC.

Exit codes: 0 success, 2 usage/argument error, 1 any other failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from core.mcp_hub import commands as mcp_commands
from core.mcp_hub import repository
from core.mcp_hub.definition import parse_add_argv, provider_entry
from core.mcp_hub.errors import McpHubError

MANAGEMENT_COMMANDS = ("add", "generate", "regenerate", "enable", "disable", "remove")

USAGE = """\
usage:
  vibe mcp --list [--all] [--format json]
  vibe mcp <server> --list [--format json]
  vibe mcp <server> <tool> --help
  vibe mcp <server> <tool> [--<key> <value> ...]
  vibe mcp <server> <tool> --args-json '<object>' | --args-file <path> | --args-stdin
  vibe mcp <server> -- <tool-starting-with-dash> ...

management (Owner):
  vibe mcp add [--transport stdio|http] [--env KEY=VALUE ...] [--header 'Name: value' ...]
               [--description TEXT] [--cwd DIR] [--replace] <name> -- <command> [args...]
  vibe mcp add --transport http <name> <url>
  vibe mcp add --json-stdin [--replace]
  vibe mcp generate|regenerate|enable|disable|remove <name>
"""


def escape(text: object) -> str:
    """Escape control characters in server-provided text for terminal output."""

    return "".join(
        char if char.isprintable() or char == " " else char.encode("unicode_escape").decode("ascii")
        for char in str(text)
    )


def _fail(error: McpHubError) -> int:
    print(f"error: {error.message}", file=sys.stderr)
    if error.hint:
        print(f"hint: {error.hint}", file=sys.stderr)
    return 2 if error.usage else 1


def _admit(argv: list[str]) -> Optional[int]:
    """Command admission: management is Owner-only, discovery and calls are Editor."""

    from vibe.authorization import InstanceAuthorizationError, require_instance_role

    floor = "owner" if argv and argv[0] in MANAGEMENT_COMMANDS else "editor"
    try:
        require_instance_role(None, floor)
    except InstanceAuthorizationError as exc:
        return _fail(McpHubError(exc.code, str(exc)))
    return None


def main(argv: list[str]) -> int:
    denied = _admit(argv)
    if denied is not None:
        return denied
    try:
        return _dispatch(argv)
    except McpHubError as exc:
        return _fail(exc)


def _dispatch(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(USAGE, end="")
        return 0 if argv else 2
    head = argv[0]
    if head == "--list" or head == "--all":
        return _root_list(argv)
    if head in MANAGEMENT_COMMANDS:
        return _manage(head, argv[1:])
    if head.startswith("-"):
        raise McpHubError("usage", f"Unknown option {head}.", hint="vibe mcp --help", usage=True)
    return _server(head, argv[1:])


def _format_json(options: list[str], allowed: set[str]) -> bool:
    as_json = False
    index = 0
    while index < len(options):
        option = options[index]
        if option == "--format" and index + 1 < len(options):
            if options[index + 1] not in {"json", "text"}:
                raise McpHubError("usage", "--format accepts json or text.", usage=True)
            as_json = options[index + 1] == "json"
            index += 2
            continue
        if option not in allowed:
            raise McpHubError("usage", f"Unexpected argument {option}.", hint="vibe mcp --help", usage=True)
        index += 1
    return as_json


def _root_list(argv: list[str]) -> int:
    as_json = _format_json(argv, {"--list", "--all"})
    packages = repository.list_packages() if "--all" in argv else repository.enabled_generated()
    if as_json:
        print(json.dumps({"servers": packages}, ensure_ascii=False, indent=2))
        return 0
    if not packages:
        print("No MCP servers." if "--all" in argv else "No enabled MCP servers. (vibe mcp --list --all)")
        return 0
    for item in packages:
        extras = [item["status"]]
        if item.get("enabled"):
            extras.append("enabled")
        if item.get("nativeDeleted"):
            extras.append(f"deleted in {item.get('source') or 'native config'}")
        elif item.get("source"):
            extras.append(f"from {item['source']}")
        state = "" if "--all" not in argv else f"  [{', '.join(extras)}]"
        print(f"{item['name']}  {escape(item.get('description') or '')}{state}".rstrip())
    print("\nNext: vibe mcp <server> --list")
    return 0


def _manage(command: str, args: list[str]) -> int:
    from vibe import internal_client

    if command == "add":
        if "--json-stdin" in args:
            position = args.index("--json-stdin")
            args = args[: position + 1] + [sys.stdin.read()] + args[position + 1 :]
        definitions, replace = parse_add_argv(args)
        payload = {
            "replace": replace,
            "servers": [
                {"name": item["name"], "server": provider_entry(item), "description": item.get("description")}
                for item in definitions
            ],
        }
        operation = "save"
    else:
        if len(args) != 1:
            raise McpHubError("usage", f"usage: vibe mcp {command} <name>", usage=True)
        payload = {"name": args[0]}
        operation = "generate" if command == "regenerate" else command
    try:
        response = internal_client.mcp_request_sync(operation, payload)
    except internal_client.InternalServerUnavailable:
        raise McpHubError("unavailable", "The Avibe service is not running.", hint="vibe start") from None
    except internal_client.InternalServerTimeout:
        raise McpHubError("unknown_outcome", "The Avibe service connection broke; check vibe mcp --list --all.") from None
    if not response.get("ok"):
        raise McpHubError.from_payload(response.get("error"))
    if operation == "save":
        names = ", ".join(response.get("saved") or [])
        print(f"Saved {names}. Next: vibe mcp generate <name>")
    elif operation == "generate":
        print(f"Generated {response['name']} revision {response['revision']} ({response['toolCount']} tools). "
              f"Next: vibe mcp {response['name']} --list")
    elif operation == "remove":
        print(f"Removed {response['name']}.")
    else:
        print(f"{response['name']} {'enabled' if response.get('enabled') else 'disabled'}.")
    return 0


def _published(name: str) -> dict[str, Any]:
    published = repository.read_published(name)
    if published is None:
        repository.read_definition(name)
        raise McpHubError("not_generated", f"{name} has not been generated yet.", hint="vibe mcp --list --all")
    return published


def _server(name: str, args: list[str]) -> int:
    from core.mcp_hub.definition import validate_name

    try:
        validate_name(name)
    except McpHubError:
        raise McpHubError("not_found", f"Unknown MCP server {name!r}.", hint="vibe mcp --list") from None
    if not args or args[0] in {"--list", "--help", "-h", "--format"}:
        return _server_list(name, args)
    if args[0] == "--":
        if len(args) < 2:
            raise McpHubError("usage", "Missing tool name after --.", usage=True)
        tool, rest = args[1], args[2:]
    elif args[0].startswith("-"):
        raise McpHubError("usage", f"Unexpected option {args[0]}.", hint=f"vibe mcp {name} --list", usage=True)
    else:
        tool, rest = args[0], args[1:]
    published = _published(name)
    command = next((item for item in published["commands"] if item["name"] == tool), None)
    if command is None:
        raise McpHubError("not_found", f"{name} has no tool {tool!r}.", hint=f"vibe mcp {name} --list")
    if rest in (["--help"], ["-h"]):
        print(mcp_commands.render_tool_help(name, command, escape))
        return 0
    arguments = mcp_commands.parse_tool_argv(
        command,
        rest,
        read_file=lambda path: Path(path).read_text(encoding="utf-8"),
        read_stdin=sys.stdin.read,
    )
    descriptor = next(item for item in published["tools"] if item["name"] == tool)
    mcp_commands.validate_arguments(descriptor, arguments)
    return _call(name, tool, arguments)


def _server_list(name: str, args: list[str]) -> int:
    as_json = _format_json(args, {"--list", "--help", "-h"})
    published = _published(name)
    if as_json:
        print(json.dumps({
            "server": name,
            "revision": published["revision"],
            "tools": [
                {**tool, "flags": command["flags"], "validated": command["validated"]}
                for tool, command in zip(published["tools"], published["commands"])
            ],
        }, ensure_ascii=False, indent=2))
        return 0
    if not repository.read_enabled(name):
        print(f"({name} is disabled; calls are rejected.)", file=sys.stderr)
    for command in published["commands"]:
        print(f"{escape(command['name'])}  {escape(command['description'])[:200]}".rstrip())
    print(f"\nNext: vibe mcp {name} <tool> --help")
    return 0


def _call(name: str, tool: str, arguments: dict[str, Any]) -> int:
    from core.caller_context import caller_context_from_env
    from vibe import internal_client

    caller = caller_context_from_env()
    payload = {"name": name, "tool": tool, "arguments": arguments, "sessionId": caller.session_id if caller else None}
    try:
        envelope = internal_client.mcp_request_sync("call", payload)
    except internal_client.InternalServerUnavailable:
        raise McpHubError("unavailable", "The Avibe service is not running; the call was not sent.", hint="vibe start") from None
    except internal_client.InternalServerTimeout:
        envelope = {
            "schemaVersion": 1, "server": name, "revision": None, "tool": tool, "status": "error",
            "error": {"type": "unknown_outcome",
                      "message": "The connection to the Avibe service broke after the call was submitted; do not retry blindly."},
        }
    if "status" not in envelope:
        raise McpHubError.from_payload(envelope.get("error"))
    print(json.dumps(envelope, ensure_ascii=False))
    if envelope["status"] == "ok":
        return 0
    error = envelope.get("error") or {}
    if error.get("hint"):
        print(f"hint: {error['hint']}", file=sys.stderr)
    return 2 if error.get("usage") else 1
