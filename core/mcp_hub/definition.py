"""Connection definitions: provider ``mcpServers`` JSON and ``vibe mcp-add`` argv.

Both entry points normalize through :func:`definition_from_provider`, so the page
and the CLI produce identical ``definition.json`` documents. Parsing never runs or
connects to anything.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from core.mcp_hub.errors import McpHubError

SCHEMA_VERSION = 1
STDIO = "stdio"
HTTP = "streamable_http"

_STDIO_TYPES = {None, "stdio"}
_HTTP_TYPES = {"http", "streamable-http", "streamable_http"}
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
DESCRIPTION_MAX = 160


def validate_name(name: object) -> str:
    """Accept any native/user name that can be a directory and a `vibe mcp` token."""

    if not isinstance(name, str) or not name or "\x00" in name or "/" in name or "\\" in name or name in {".", ".."}:
        raise McpHubError(
            "invalid_name",
            "Server names cannot be empty, `.`/`..`, or contain `/`, `\\`, or NUL.",
            field="name",
            usage=True,
        )
    if name in {".hidden.json", "hidden.json"}:
        raise McpHubError(
            "invalid_name",
            "Server name hidden.json is reserved for Avibe MCP Hub metadata.",
            field="name",
            usage=True,
        )
    return name


def _invalid(field: str, message: str) -> McpHubError:
    return McpHubError("invalid_definition", message, field=field, usage=True)


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise _invalid(field, f"{field} must be a non-empty string.")
    if "\x00" in value:
        raise _invalid(field, f"{field} must not contain NUL characters.")
    if _PLACEHOLDER.search(value):
        raise _invalid(
            field,
            f"{field} uses a ${{VAR}} placeholder; MCP Hub does not expand variables, enter the literal value.",
        )
    return value


def _string_map(value: object, field: str, key_pattern: re.Pattern[str]) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _invalid(field, f"{field} must be an object of string values.")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key_pattern.match(key):
            raise _invalid(f"{field}.{key}", f"{field} has an invalid name: {key!r}.")
        result[key] = _text(item, f"{field}.{key}", allow_empty=True)
    return result


def normalize_description(value: object) -> str:
    """One line, at most 160 characters; the server-reported prompt description."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise _invalid("description", "description must be a string.")
    return " ".join(value.split())[:DESCRIPTION_MAX]


def definition_from_provider(name: str, server: object) -> dict[str, Any]:
    """Normalize one provider ``mcpServers`` entry into a persisted definition."""

    validate_name(name)
    if not isinstance(server, dict):
        raise _invalid("server", f"mcpServers.{name} must be an object.")
    # Backend-owned extras (env_vars, timeouts, …) are ignored, not rejected.
    kind = server.get("type")
    if kind == "sse":
        raise McpHubError(
            "unsupported_transport_sse",
            "SSE transport is not supported; use a Streamable HTTP URL.",
            field="type",
            usage=True,
        )
    has_command = "command" in server
    has_url = "url" in server
    if has_command and has_url:
        raise _invalid("url", "Use either command (stdio) or url (Streamable HTTP), not both.")
    definition: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "name": name,
    }
    if has_url or kind in _HTTP_TYPES:
        if kind not in _HTTP_TYPES | {None}:
            raise _invalid("type", f"Unsupported transport type {kind!r}.")
        for field in ("args", "env", "cwd"):
            if field in server:
                raise _invalid(field, f"{field} applies only to stdio servers.")
        definition["transport"] = {
            "type": HTTP,
            "url": _validate_url(server.get("url")),
            "headers": _string_map(server.get("headers"), "headers", _HEADER_NAME),
        }
        oauth = server.get("oauth")
        if isinstance(oauth, dict):
            selected = {key: oauth[key] for key in ("clientId", "scopes") if key in oauth and isinstance(oauth[key], str)}
            if selected:
                definition["oauth"] = selected
        return definition
    if kind not in _STDIO_TYPES:
        raise _invalid("type", f"Unsupported transport type {kind!r}.")
    if "headers" in server:
        raise _invalid("headers", "headers apply only to HTTP servers.")
    args = server.get("args", [])
    if not isinstance(args, list):
        raise _invalid("args", "args must be an array of strings.")
    transport: dict[str, Any] = {
        "type": STDIO,
        "command": _text(server.get("command"), "command"),
        "args": [_text(item, f"args[{index}]", allow_empty=True) for index, item in enumerate(args)],
        "env": _string_map(server.get("env"), "env", _ENV_KEY),
    }
    if server.get("cwd") is not None:
        cwd = _text(server["cwd"], "cwd")
        if not os.path.isabs(cwd):
            raise _invalid("cwd", "cwd must be an absolute path.")
        transport["cwd"] = cwd
    definition["transport"] = transport
    return definition


def _validate_url(value: object) -> str:
    url = _text(value, "url")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise _invalid("url", "url must be an http(s) URL.")
    if parts.username is not None or parts.password is not None:
        raise _invalid("url", "url must not embed credentials; put them in an Authorization header.")
    return url


def parse_provider_json(text: str) -> list[dict[str, Any]]:
    """Parse a pasted ``{"mcpServers": {...}}`` block into definitions (sorted by name)."""

    try:
        document = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise _invalid("json", f"Invalid JSON: {exc}") from None
    if not isinstance(document, dict) or not isinstance(document.get("mcpServers"), dict):
        raise _invalid("mcpServers", 'Expected an object with an "mcpServers" map.')
    servers = document["mcpServers"]
    if not servers:
        raise _invalid("mcpServers", "mcpServers is empty.")
    parsed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    first_error: McpHubError | None = None
    for name in sorted(servers):
        try:
            parsed.append(definition_from_provider(name, servers[name]))
        except McpHubError as cop:
            if first_error is None:
                first_error = cop
            failures.append({"name": str(name), "error": cop.message, "type": cop.type})
    if not parsed:
        if len(failures) == 1 and first_error is not None:
            raise first_error
        raise McpHubError(
            "invalid_definition",
            "; ".join(f"{item['name']}: {item['error']}" for item in failures),
            usage=True,
        )
    return ProviderBatch(parsed, failures)


class ProviderBatch(list):
    """Parsed definitions plus any entry-level failures that did not abort the document."""

    def __init__(self, definitions: list[dict[str, Any]], failures: list[dict[str, str]]):
        super().__init__(definitions)
        self.failures = failures


# ---------------------------------------------------------------------------
# ``vibe mcp-add`` (Claude Code ``mcp add`` subset plus Avibe extensions)
# ---------------------------------------------------------------------------

_UNSUPPORTED_ADD_OPTIONS = {"--scope", "-s", "--callback-port", "--client-id", "--client-secret"}


def parse_add_argv(argv: list[str]) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(definitions, replace)`` for ``vibe mcp-add`` arguments.

    ``--json-stdin`` is resolved by the caller, which passes the stdin text as
    the single token after ``--json-stdin``.
    """

    if "--" in argv:
        split = argv.index("--")
        head, command = argv[:split], argv[split + 1 :]
    else:
        head, command = argv, None
    transport = "stdio"
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    cwd: Optional[str] = None
    replace = False
    json_text: Optional[str] = None
    positionals: list[str] = []
    index = 0

    def value_of(option: str) -> str:
        nonlocal index
        if index + 1 >= len(head):
            raise McpHubError("usage", f"{option} requires a value.", usage=True)
        index += 1
        return head[index]

    while index < len(head):
        token = head[index]
        option, eq, inline = token.partition("=") if token.startswith("--") else (token, "", "")
        if option in _UNSUPPORTED_ADD_OPTIONS:
            raise McpHubError("unsupported_option", f"{option} is not supported by vibe mcp-add.", usage=True)
        if option in {"--transport", "-t"}:
            transport = inline if eq else value_of(option)
        elif option in {"--env", "-e"}:
            # Claude's --env is variadic: it takes every following KEY=VALUE.
            values = [inline] if eq else []
            while index + 1 < len(head) and "=" in head[index + 1] and not head[index + 1].startswith("-"):
                index += 1
                values.append(head[index])
            if not values:
                raise McpHubError("usage", "--env requires KEY=VALUE.", usage=True)
            for item in values:
                key, sep, val = item.partition("=")
                if not sep:
                    raise McpHubError("usage", f"--env expects KEY=VALUE, got {item!r}.", usage=True)
                env[key] = val
        elif option in {"--header", "-H"}:
            item = inline if eq else value_of(option)
            key, sep, val = item.partition(":")
            if not sep:
                raise McpHubError("usage", f"--header expects 'Name: value', got {item!r}.", usage=True)
            headers[key.strip()] = val.strip()
        elif option == "--cwd":
            cwd = inline if eq else value_of(option)
        elif option == "--replace":
            replace = True
        elif option == "--json-stdin":
            json_text = value_of(option)
        elif token.startswith("-") and token != "-":
            raise McpHubError("usage", f"Unknown option {token}.", usage=True)
        else:
            positionals.append(token)
        index += 1

    if json_text is not None:
        if positionals or command is not None or env or headers or cwd:
            raise McpHubError("usage", "--json-stdin takes no other server arguments.", usage=True)
        return parse_provider_json(json_text), replace
    if not positionals:
        raise McpHubError("usage", "Missing server name.", usage=True)
    name, rest = positionals[0], positionals[1:]
    kind = {"stdio": None, "http": "http", "sse": "sse"}.get(transport)
    if transport not in {"stdio", "http", "sse"}:
        raise McpHubError("usage", f"Unsupported --transport {transport!r}; use stdio or http.", usage=True)
    server: dict[str, Any] = {} if kind is None else {"type": kind}
    if kind is None:
        argv_command = command if command is not None else rest
        if command is not None and rest:
            raise McpHubError("usage", "Put the server command after --.", usage=True)
        if not argv_command:
            raise McpHubError("usage", "Missing server command (vibe mcp-add <name> -- <command> [args...]).", usage=True)
        server.update(command=argv_command[0], args=argv_command[1:])
        if env:
            server["env"] = env
        if cwd is not None:
            server["cwd"] = cwd
        if headers:
            server["headers"] = headers
    else:
        if command is not None or len(rest) != 1:
            raise McpHubError("usage", "HTTP servers take exactly one URL: vibe mcp-add --transport http <name> <url>.", usage=True)
        server["url"] = rest[0]
        if headers:
            server["headers"] = headers
        if env:
            server["env"] = env
        if cwd is not None:
            server["cwd"] = cwd
    return [definition_from_provider(name, server)], replace


def provider_entry(definition: dict[str, Any]) -> dict[str, Any]:
    """The provider-shape entry for a definition (inverse of normalization)."""

    transport = definition["transport"]
    if transport["type"] == HTTP:
        entry: dict[str, Any] = {"type": "http", "url": transport["url"]}
        if transport.get("headers"):
            entry["headers"] = transport["headers"]
        if definition.get("oauth"):
            entry["oauth"] = dict(definition["oauth"])
        return entry
    entry = {"command": transport["command"], "args": list(transport.get("args") or [])}
    for field in ("env", "cwd"):
        if transport.get(field):
            entry[field] = transport[field]
    return entry

