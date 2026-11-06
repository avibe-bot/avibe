"""The backends' own MCP config files are the Hub's connection store.

Hub reads the union of:

- Claude ``~/.claude.json`` (or ``$CLAUDE_CONFIG_DIR/.claude.json``) ``mcpServers``
- Codex ``$CODEX_HOME/config.toml`` (default ``~/.codex``) ``[mcp_servers.<name>]``
- OpenCode ``~/.config/opencode/opencode.json`` ``mcp``

as a union. Avibe-added servers live under ``<avibe-home>/mcp``. The Hub
never edits these files except :func:`remove_entry`, which deletes one named
entry and leaves every other byte in place.
For a name present in several files the first source in that order is canonical.
Avibe sessions never load these servers natively; see
the Codex transport and :func:`opencode_disabled_mcp`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
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
_OPENCODE_KEPT = {"enabled", "disabled", "timeout"}


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
        data = _parse(source, text)
    except Exception as exc:
        raise _unreadable(source, path, exc) from None
    if not isinstance(data, dict):
        raise _unreadable(source, path, TypeError("not an object"))
    return path, data



def _parse(source: str, text: str) -> Any:
    if source == CODEX:
        try:
            import tomllib  # type: ignore[attr-defined]
        except ImportError:  # pragma: no cover - py<3.11
            import tomli as tomllib  # type: ignore[no-redef]
        return tomllib.loads(text)
    if source == OPENCODE:
        from vibe.opencode_config import parse_jsonc_object

        return parse_jsonc_object(text)
    return json.loads(text)


def _servers(source: str, data: dict[str, Any]) -> dict[str, Any]:
    value = data.get(_KEY[source])
    if source == OPENCODE and _opencode_v2(data):
        return value["servers"]
    return value if isinstance(value, dict) else {}


def _opencode_v2(data: dict[str, Any]) -> bool:
    """Distinguish the V2 container from a legacy server named ``servers``."""

    mcp = data.get("mcp")
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    return isinstance(servers, dict) and not (
        isinstance(servers.get("type"), str)
        or isinstance(servers.get("command"), list)
        or isinstance(servers.get("url"), str)
    )


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
    project: bool = False


def _native_enabled(source: str, entry: dict[str, Any]) -> bool:
    """The backend's own off switch. Claude has none; Codex/OpenCode use ``enabled``."""

    if source == CLAUDE:
        return True
    return entry.get("enabled") is not False and (source != OPENCODE or entry.get("disabled") is not True)


def _from_native(source: str, name: str, entry: dict[str, Any], path: Path) -> dict[str, Any]:
    server = _READERS[source](entry)
    cwd = server.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        server["cwd"] = _resolve_cwd(path, cwd)
    return definition_from_provider(name, server)


def read_all() -> dict[str, NativeServer]:
    """The union of native servers; the first source holding a name is canonical.

    A file that cannot be parsed is skipped; one bad entry only marks that server.
    Native ``enabled=false`` entries are omitted from Hub lists, matching the
    backend's own off switch. Spawn isolation still disables remaining names.
    """

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
                definition = _from_native(source, str(name), entry, path)
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


def _project_codex_path(root: Path) -> Path:
    return root / ".codex" / "config.toml"


def _project_opencode_paths(root: Path) -> list[Path]:
    return [
        root / "opencode.json",
        root / "opencode.jsonc",
        root / ".opencode" / "opencode.json",
        root / ".opencode" / "opencode.jsonc",
    ]


def read_project(root: Path) -> dict[str, NativeServer]:
    """Read only the explicitly selected project's native files.

    This intentionally does not walk ancestors or subdirectories. The Hub's
    project order is `.mcp.json`, Claude local, Codex, then OpenCode.
    """
    root = root.resolve()
    result: dict[str, NativeServer] = {}

    def add(source: str, path: Path, entries: dict[str, Any]) -> None:
        for name, entry in entries.items():
            if name in result:
                continue
            try:
                if not isinstance(entry, dict):
                    raise McpHubError("invalid_definition", f"{source} entry {name!r} is not an object.")
                if not _native_enabled(source, entry):
                    continue
                result[str(name)] = NativeServer(str(name), source, path, _from_native(source, str(name), entry, path), None, "cwd" in entry, True)
            except McpHubError as exc:
                result[str(name)] = NativeServer(str(name), source, path, None, exc, isinstance(entry, dict) and "cwd" in entry, True)

    # Claude local scope is keyed by canonical project root.
    try:
        path, data = _load(CLAUDE)
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        local = projects.get(str(root), {})
        add(CLAUDE, path, local.get("mcpServers", {}) if isinstance(local, dict) else {})
    except McpHubError:
        pass

    # Codex project config is active only for an explicitly trusted root.
    try:
        _path, global_data = _load(CODEX)
        projects = global_data.get("projects") if isinstance(global_data.get("projects"), dict) else {}
        trusted = projects.get(str(root), {})
        trusted = isinstance(trusted, dict) and trusted.get("trust_level") == "trusted"
        path = _project_codex_path(root)
        if trusted and path.exists():
            data = _parse(CODEX, path.read_text(encoding="utf-8"))
            add(CODEX, path, _servers(CODEX, data))
    except (OSError, ValueError, McpHubError) as exc:
        logger.warning("Skipping Codex project config for %s: %s", root, exc)

    # OpenCode V2 project files accumulate unique names; nearest file wins.
    for path in reversed(_project_opencode_paths(root)):
        if not path.exists():
            continue
        try:
            data = _parse(OPENCODE, path.read_text(encoding="utf-8"))
            add(OPENCODE, path, _servers(OPENCODE, data))
        except (OSError, ValueError, McpHubError) as exc:
            logger.warning("Skipping OpenCode project config %s: %s", path, exc)
            continue
    return result


def disabled_source(name: str) -> Optional[str]:
    """The first source holding ``name`` as a native ``enabled=false`` entry."""

    for source in SOURCES:
        try:
            _path, data = _load(source)
        except McpHubError:
            continue
        entry = _servers(source, data).get(name)
        if isinstance(entry, dict) and not _native_enabled(source, entry):
            return source
    return None


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
# Delete: remove one entry from its native file
# ---------------------------------------------------------------------------

_KEY = {CLAUDE: "mcpServers", CODEX: "mcp_servers", OPENCODE: "mcp"}
# One read-modify-write at a time, so two deletes from one file never lose an edit.
_EDIT_LOCK = threading.Lock()


def _toml_header_path(line: str) -> Optional[tuple[str, ...]]:
    """Key path of a ``[table]`` / ``[[array]]`` header line, else ``None``."""

    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    inner = stripped[2:] if stripped.startswith("[[") else stripped[1:]
    close = inner.find("]]" if stripped.startswith("[[") else "]")
    if close < 0:
        return None
    try:
        node: Any = _parse(CODEX, f"{inner[:close]} = 0")
    except Exception:
        return None
    path: list[str] = []
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        path.append(key)
    return tuple(path)


def _remove_codex(text: str, name: str) -> str:
    """Drop ``[mcp_servers.<name>]`` and its sub-tables, line for line."""

    kept: list[str] = []
    dropping = False
    for line in text.splitlines(keepends=True):
        header = _toml_header_path(line)
        if header is not None:
            dropping = header[:2] == (_KEY[CODEX], name)
        if not dropping:
            kept.append(line)
    return "".join(kept)


def _remove_text(source: str, text: str, name: str) -> str:
    if source == CODEX:
        return _remove_codex(text, name)
    from vibe.opencode_config import remove_jsonc_member

    parent = ("mcp", "servers") if source == OPENCODE and _opencode_v2(_parse(source, text)) else _KEY[source]
    return remove_jsonc_member(text, parent, name)


def remove_entry(source: str, name: str, *, path: Optional[Path] = None) -> bool:
    """Delete ``name`` from ``source``'s native file; ``False`` if it is already gone.

    Re-reads the file, so an edit since discovery is respected. The result must
    parse to the original minus that one entry, or nothing is written.
    """

    with _EDIT_LOCK:
        return _remove_entry_locked(source, name, path=path)


def remove_project_entry(server: NativeServer, root: Path) -> bool:
    """Remove one root-project declaration from a native project source."""
    path = server.path
    if server.source != CLAUDE:
        return _remove_entry_locked(server.source, server.name, path=path)
    text = path.read_text(encoding="utf-8")
    data = _parse(server.source, text)
    projects = data.get("projects", {})
    project = projects.get(str(root.resolve()), {}) if isinstance(projects, dict) else {}
    servers = project.get("mcpServers", {}) if isinstance(project, dict) else {}
    if server.name not in servers:
        return False
    del servers[server.name]
    from vibe.opencode_config import remove_jsonc_member
    updated = remove_jsonc_member(text, ("projects", str(root.resolve()), "mcpServers"), server.name)
    expected = json.loads(json.dumps(data))
    del expected["projects"][str(root.resolve())]["mcpServers"][server.name]
    if _parse(server.source, updated) != expected:
        raise McpHubError("invalid_native_config", f"could not remove {server.name!r} without changing other content")
    from config.atomic_io import write_atomic
    write_atomic(path, updated, follow_symlinks=True, mode=path.stat().st_mode & 0o777)
    return True


def _remove_entry_locked(source: str, name: str, *, path: Optional[Path] = None) -> bool:
    from config.atomic_io import write_atomic

    if path is None:
        path, _data = _load(source)
    else:
        _data = _parse(source, path.read_text(encoding="utf-8"))
    try:
        text = path.read_text(encoding="utf-8")
        data = _parse(source, text)
    except FileNotFoundError:
        return False
    except Exception as exc:
        raise _unreadable(source, path, exc) from None
    if not isinstance(data, dict) or name not in _servers(source, data):
        return False
    try:
        updated = _remove_text(source, text, name)
        expected = json.loads(json.dumps(data, default=str))
        _servers(source, expected).pop(name)
        actual = json.loads(json.dumps(_parse(source, updated), default=str))
        if expected[_KEY[source]] == {} and _KEY[source] not in actual:
            expected.pop(_KEY[source])  # an implicit TOML parent table goes with its last child
        if actual != expected:
            raise ValueError("entry is not a standalone block")
    except Exception as exc:
        raise McpHubError(
            "invalid_native_config",
            f"{source} config {path}: could not remove {name!r} without touching other content ({exc}).",
            hint=f"Remove it from {path} by hand.",
        ) from None

    def unchanged() -> None:
        if path.read_text(encoding="utf-8") != text:
            raise McpHubError("native_config_changed", f"{path} changed while removing {name!r}; try again.")

    write_atomic(path, updated, follow_symlinks=True, mode=path.stat().st_mode & 0o777, before_replace=unchanged)
    return True


# ---------------------------------------------------------------------------
# Spawn isolation: Avibe sessions never load native MCP servers
# ---------------------------------------------------------------------------


def _names(source: str) -> list[str]:
    try:
        _path, data = _load(source)
    except McpHubError:
        return []
    return sorted(str(name) for name in _servers(source, data))


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
