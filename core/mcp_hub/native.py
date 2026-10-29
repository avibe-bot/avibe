"""The backends' own MCP config files are the Hub's connection store.

Hub reads the union of:

- Claude ``~/.claude.json`` (or ``$CLAUDE_CONFIG_DIR/.claude.json``) ``mcpServers``
- Codex ``$CODEX_HOME/config.toml`` (default ``~/.codex``) ``[mcp_servers.<name>]``
- OpenCode ``~/.config/opencode/opencode.json`` ``mcp``

as a read-only union. Avibe-added servers live under ``<avibe-home>/mcp``.
For a name present in several files the first source in that order is canonical.
Avibe sessions never load these servers natively; see
:func:`codex_disable_overrides` and :func:`opencode_disabled_mcp`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from core.mcp_hub.definition import definition_from_provider
from core.mcp_hub.errors import McpHubError

logger = logging.getLogger(__name__)

CLAUDE, CODEX, OPENCODE = "claude", "codex", "opencode"
SOURCES = (CLAUDE, CODEX, OPENCODE)

# Native keys that carry no connection meaning for the Hub; kept on rewrite.
_CODEX_KEPT = {"enabled", "startup_timeout_sec", "startup_timeout_ms", "tool_timeout_sec", "enabled_tools", "disabled_tools", "env_vars"}
_OPENCODE_KEPT = {"enabled", "timeout"}


def claude_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home()) / ".claude.json"


def codex_path() -> Path:
    from vibe.codex_config import get_codex_config_paths

    return get_codex_config_paths()[0]


def opencode_path() -> Path:
    from vibe.opencode_config import get_opencode_config_paths

    candidates = get_opencode_config_paths()
    return next((path for path in candidates if path.exists()), candidates[0])


# ---------------------------------------------------------------------------
# File I/O (a file that cannot be parsed is never rewritten)
# ---------------------------------------------------------------------------


def _unreadable(source: str, path: Path, exc: Exception) -> McpHubError:
    return McpHubError("invalid_native_config", f"{source} config {path} could not be parsed: {type(exc).__name__}")


def _load(source: str) -> tuple[Path, dict[str, Any]]:
    path = {CLAUDE: claude_path, CODEX: codex_path, OPENCODE: opencode_path}[source]()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return path, {}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _unreadable(source, path, exc) from None
    try:
        if source == CODEX:
            try:
                import tomllib  # type: ignore[attr-defined]
            except ImportError:  # pragma: no cover - py<3.11
                import tomli as tomllib  # type: ignore[no-redef]
            data = tomllib.loads(text)
        elif source == OPENCODE:
            from vibe.opencode_config import parse_jsonc_object

            data = parse_jsonc_object(text)
        else:
            data = json.loads(text)
    except Exception as exc:
        raise _unreadable(source, path, exc) from None
    if not isinstance(data, dict):
        raise _unreadable(source, path, TypeError("not an object"))
    return path, data



def _servers(source: str, data: dict[str, Any]) -> dict[str, Any]:
    key = {CLAUDE: "mcpServers", CODEX: "mcp_servers", OPENCODE: "mcp"}[source]
    value = data.get(key)
    return value if isinstance(value, dict) else {}



# ---------------------------------------------------------------------------
# Native entry <-> provider shape
# ---------------------------------------------------------------------------


def _from_codex(entry: dict[str, Any]) -> dict[str, Any]:
    server = {key: value for key, value in entry.items() if key not in _CODEX_KEPT}
    if "http_headers" in server:
        server["headers"] = server.pop("http_headers")
    copied = _env_from_codex_env_vars(entry.get("env_vars"))
    if copied:
        explicit = server.get("env") if isinstance(server.get("env"), dict) else {}
        server["env"] = {**copied, **{key: value for key, value in explicit.items() if isinstance(key, str)}}
    return server


def _env_from_codex_env_vars(value: object) -> dict[str, str]:
    """Copy named parent-env keys into the server process; missing names are skipped."""

    if not isinstance(value, list):
        return {}
    copied: dict[str, str] = {}
    for item in value:
        if isinstance(item, str):
            name = item
            source = "local"
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            name = item["name"]
            raw = item.get("source")
            source = raw if isinstance(raw, str) and raw else "local"
        else:
            continue
        if source != "local" or name not in os.environ:
            continue
        copied[name] = os.environ[name]
    return copied


def _from_opencode(entry: dict[str, Any]) -> dict[str, Any]:
    server = {key: value for key, value in entry.items() if key not in _OPENCODE_KEPT}
    kind = server.pop("type", None)
    if kind == "local":
        command = server.pop("command", None)
        if not isinstance(command, list) or not command:
            raise McpHubError("invalid_definition", "OpenCode local servers need a command array.", field="command")
        server["command"], server["args"] = command[0], command[1:]
        if "environment" in server:
            server["env"] = server.pop("environment")
    elif kind == "remote":
        server["type"] = "http"
    else:
        raise McpHubError("invalid_definition", f"Unsupported OpenCode MCP type {kind!r}.", field="type")
    return server


def _resolve_cwd(path: Path, cwd: str) -> str:
    """A native cwd, made absolute: relative values anchor at the holding file's directory."""

    expanded = os.path.expanduser(cwd)
    return expanded if os.path.isabs(expanded) else os.path.normpath(os.path.join(path.parent, expanded))



_READERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    CLAUDE: lambda entry: dict(entry),
    CODEX: _from_codex,
    OPENCODE: _from_opencode,
}


# ---------------------------------------------------------------------------
# Public store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeServer:
    name: str
    source: str
    path: Path
    definition: Optional[dict[str, Any]]
    error: Optional[McpHubError]
    has_cwd: bool = False


def _native_enabled(source: str, entry: dict[str, Any]) -> bool:
    """The backend's own off switch. Claude has none; Codex/OpenCode use ``enabled``."""

    if source == CLAUDE:
        return True
    return entry.get("enabled") is not False


def _from_native(source: str, name: str, entry: dict[str, Any], path: Path, description: Optional[str]) -> dict[str, Any]:
    server = _READERS[source](entry)
    cwd = server.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        server["cwd"] = _resolve_cwd(path, cwd)
    return definition_from_provider(name, server, description=description)


def read_all(descriptions: Optional[dict[str, str]] = None) -> dict[str, NativeServer]:
    """The union of native servers; the first source holding a name is canonical.

    A file that cannot be parsed is skipped; one bad entry only marks that server.
    Native ``enabled=false`` entries are omitted from Hub lists, matching the
    backend's own off switch. Spawn isolation still disables remaining names.
    """

    descriptions = descriptions or {}
    servers: dict[str, NativeServer] = {}
    for source in SOURCES:
        try:
            path, data = _load(source)
        except McpHubError as exc:
            # An unparseable file hides only its own servers and is never rewritten.
            logger.warning("%s", exc.message)
            continue
        for name, entry in _servers(source, data).items():
            if name in servers:
                continue
            try:
                if not isinstance(entry, dict):
                    raise McpHubError("invalid_definition", f"{source} entry {name!r} is not an object.")
                if not _native_enabled(source, entry):
                    continue
                definition = _from_native(source, str(name), entry, path, descriptions.get(str(name)))
                servers[str(name)] = NativeServer(
                    str(name), source, path, definition, None, has_cwd="cwd" in entry
                )
            except McpHubError as cop:
                if isinstance(entry, dict) and not _native_enabled(source, entry):
                    continue
                servers[str(name)] = NativeServer(
                    str(name),
                    source,
                    path,
                    None,
                    cop,
                    has_cwd=isinstance(entry, dict) and "cwd" in entry,
                )
    return servers


def disabled_names() -> set[str]:
    """Names whose native entry is ``enabled=false``. Hub lists omit these."""

    names: set[str] = set()
    for source in SOURCES:
        try:
            _path, data = _load(source)
        except McpHubError:
            continue

        for name, entry in _servers(source, data).items():
            if isinstance(entry, dict) and not _native_enabled(source, entry):
                names.add(str(name))
    return names



# ---------------------------------------------------------------------------
# Spawn isolation: Avibe sessions never load native MCP servers
# ---------------------------------------------------------------------------


def _names(source: str) -> list[str]:
    try:
        _path, data = _load(source)
    except McpHubError:
        return []
    return sorted(str(name) for name in _servers(source, data))


def codex_disable_overrides() -> list[str]:
    """``-c`` overrides that disable every user ``[mcp_servers]`` entry.

    Codex merges ``-c`` values into its tables, so an empty table cannot remove
    them; ``enabled=false`` per server does, without touching the user file.
    """

    from vibe.codex_config import _format_toml_key

    return [f"mcp_servers.{_format_toml_key(name)}.enabled=false" for name in _names(CODEX)]


def opencode_disabled_mcp() -> dict[str, Any]:
    """Runtime-override ``mcp`` entries that disable every user OpenCode server.

    Each entry is the full native entry with ``enabled: false`` so the override
    stays schema-valid on its own.
    """

    try:
        _path, data = _load(OPENCODE)
    except McpHubError:
        return {}
    return {
        name: {**entry, "enabled": False}
        for name, entry in _servers(OPENCODE, data).items()
        if isinstance(entry, dict)
    }
