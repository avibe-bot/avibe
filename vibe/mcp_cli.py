"""``vibe mcp``: per-server discovery and calls. Management is ``vibe mcp-add`` / ``vibe mcp-discover``.

List and help read local artifacts only. Calls and management go to the Controller
over control IPC.

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

USAGE = """\
usage:
  vibe mcp --list [--format json]
  vibe mcp <server> --list [--format json]
  vibe mcp <server> --refresh
  vibe mcp <server> --remove
  vibe mcp <server> <tool> --help
  vibe mcp <server> <tool> [--<key> <value> ...]
  vibe mcp <server> <tool> --args-json '<object>' | --args-file <path> | --args-stdin
  vibe mcp <server> -- <tool-starting-with-dash> ...

management (Owner):
  vibe mcp-add [--transport stdio|http] [--env KEY=VALUE ...] [--header 'Name: value' ...]
               [--description TEXT] [--cwd DIR] [--replace] <name> -- <command> [args...]
  vibe mcp-add --transport http <name> <url>
  vibe mcp-add --json-stdin [--replace]
  vibe mcp-discover
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


def _admit(floor: str) -> Optional[int]:
    from vibe.authorization import InstanceAuthorizationError, require_instance_role

    try:
        require_instance_role(None, floor)
    except InstanceAuthorizationError as cop:
        return _fail(McpHubError(cop.code, str(cop)))
    return None


def main(argv: list[str]) -> int:
    denied = _admit("editor")
    if denied is not None:
        return denied
    try:
        return _dispatch(argv)
    except McpHubError as cop:
        return _fail(cop)


def add_main(argv: list[str]) -> int:
    if argv and argv[0] in {"-h", "--help"}:
        print(USAGE, end="")
        return 0
    denied = _admit("owner")
    if denied is not None:
        return denied
    try:
        return _add(argv)
    except McpHubError as cop:
        return _fail(cop)


def discover_main(argv: list[str]) -> int:
    if argv and argv[0] in {"-h", "--help"}:
        print("usage: vibe mcp-discover\n", end="")
        return 0
    denied = _admit("owner")
    if denied is not None:
        return denied
    if argv:
        raise_or = McpHubError("usage", "usage: vibe mcp-discover", usage=True)
        return _fail(raise_or)
    try:
        return _owner("discover", {})
    except McpHubError as cop:
        return _fail(cop)


def _dispatch(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE, end="")
        return 0 if argv else 2
    head = argv[0]
    if head == "--list":
        return _root_list(argv)
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
    as_json = _format_json(argv, {"--list"})
    packages = repository.list_packages()
    if as_json:
        print(json.dumps({"servers": packages}, ensure_ascii=False, indent=2))
        return 0
    if not packages:
        print("No MCP servers.")
        return 0
    for item in packages:
        extra = f"  [{item.get('source') or 'avibe'}]" if item.get("source") else ""
        print(f"{item['name']}  {escape(item.get('description') or '')}{extra}".rstrip())
    print("\nNext: vibe mcp <server> --list")
    return 0


def _owner(operation: str, payload: dict[str, Any]) -> int:
    from vibe import internal_client

    try:
        response = internal_client.mcp_request_sync(operation, payload)
    except internal_client.InternalServerUnavailable:
        raise McpHubError("unavailable", "The Avibe service is not running.", hint="vibe start") from None
    except internal_client.InternalServerTimeout:
        raise McpHubError("unknown_outcome", "The Avibe service connection broke; check vibe mcp --list.") from None
    if not response.get("ok") and not (operation in {"add", "save"} and "failed" in response):
        raise McpHubError.from_payload(response.get("error"))
    if operation == "discover":
        for item in response.get("results") or []:
            if item.get("status") == "registered":
                print(f"{item['name']}: registered ({item.get('toolCount', 0)} tools)")
            else:
                print(f"{item['name']}: failed — {item.get('error') or 'unknown'}", file=sys.stderr)
        return 0
    if operation in {"add", "save"}:
        added = response.get("added") or []
        failed = response.get("failed") or []
        unchanged = response.get("unchanged") or []
        for item in added:
            print(f"{item['name']}: added ({item.get('toolCount', 0)} tools)")
        for item in unchanged:
            print(f"{item['name']}: unchanged")
        for item in failed:
            print(f"{item['name']}: failed — {item.get('error') or 'unknown'}", file=sys.stderr)
        succeeded = len(added) + len(unchanged)
        print(f"{succeeded} succeeded, {len(failed)} failed")
        if not added and not failed and not unchanged:
            names = ", ".join(response.get("saved") or [response.get("name") or ""])
            print(f"Registered {names} ({response.get('toolCount', 0)} tools).")
        return 0 if added or unchanged or response.get("ok") else 1
    elif operation == "refresh":
        print(f"Refreshed {response['name']} ({response.get('toolCount', 0)} tools).")
    elif operation == "remove":
        kind = response.get("kind")
        extra = " (hidden in Avibe)" if kind == "native" else ""
        print(f"Removed {response['name']}{extra}.")
    return 0


def _add(args: list[str]) -> int:
    split = args.index("--") if "--" in args else len(args)
    if "--json-stdin" in args[:split]:
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
    code = _owner("add", payload)
    parse_failed = getattr(definitions, "failures", None) or []
    if parse_failed:
        for item in parse_failed:
            print(f"error: {item['name']}: {item['error']}", file=sys.stderr)
        if code == 0:
            return 1
    return code


def _published(name: str) -> dict[str, Any]:
    published = repository.read_published(name)
    if published is None:
        raise McpHubError("not_found", f"Unknown MCP server {name!r}.", hint="vibe mcp --list")
    return published


def _server(name: str, args: list[str]) -> int:
    from core.mcp_hub.definition import validate_name

    try:
        validate_name(name)
    except McpHubError:
        raise McpHubError("not_found", f"Unknown MCP server {name!r}.", hint="vibe mcp --list") from None
    if args and args[0] == "--refresh":
        denied = _admit("owner")
        if denied is not None:
            return denied
        if len(args) != 1:
            raise McpHubError("usage", "usage: vibe mcp <server> --refresh", usage=True)
        return _owner("refresh", {"name": name})
    if args and args[0] == "--remove":
        denied = _admit("owner")
        if denied is not None:
            return denied
        if len(args) != 1:
            raise McpHubError("usage", "usage: vibe mcp <server> --remove", usage=True)
        return _owner("remove", {"name": name})
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
