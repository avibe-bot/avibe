"""On-disk MCP packages under ``<avibe-home>/mcp/<name>/``.

Connection definitions live in the backends' native MCP config files
(``core/mcp_hub/native.py``); ``state.json`` (enabled, description) is the only
Avibe-side authority. Revisions are
derived data published by writing the full revision directory and then atomically
replacing ``current.json``. Every file is written with ``write_atomic``.

Writers are serialized per package by the owner (``core/mcp_hub/runtime.py``);
readers go through :func:`read_published`, which tolerates a concurrent publish.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import paths
from config.atomic_io import write_atomic
from core.mcp_hub import native
from core.mcp_hub.definition import validate_name
from core.mcp_hub.errors import McpHubError
from core.private_fs import ensure_private_directory

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1


def mcp_root() -> Path:
    return paths.get_vibe_remote_dir() / "mcp"


def package_dir(name: str) -> Path:
    return mcp_root() / validate_name(name)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    write_atomic(path, json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def _ensure_root() -> Path:
    root = mcp_root()
    ensure_private_directory(root)
    return root


def _not_found(name: str) -> McpHubError:
    return McpHubError("not_found", f"MCP server {name!r} does not exist.", hint="vibe mcp --list --all")


# ---------------------------------------------------------------------------
# Authorities
# ---------------------------------------------------------------------------


def _state(name: str) -> dict[str, Any]:
    path = package_dir(name) / "state.json"
    try:
        value = _read_json(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise McpHubError("invalid_package", f"{name}: state.json is unreadable: {exc}") from None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("enabled", False), bool)
        or not isinstance(value.get("description", ""), str)
        or not isinstance(value.get("source", ""), str)
    ):
        raise McpHubError("invalid_package", f"{name}: state.json has an unsupported shape.")
    return value


def _native_servers() -> dict[str, native.NativeServer]:
    """Native servers, each valid definition carrying its Avibe-side description."""

    servers = native.read_all()
    for server in servers.values():
        if server.definition is not None:
            try:
                server.definition["description"] = _state(server.name).get("description", "")
            except McpHubError:
                pass
    return servers


def read_definition(name: str, servers: Optional[dict[str, native.NativeServer]] = None) -> dict[str, Any]:
    """The connection definition, read from the backend's native config file."""

    server = (_native_servers() if servers is None else servers).get(name)
    if server is None:
        raise _not_found(name)
    if server.error is not None:
        raise server.error
    return server.definition


def read_enabled(name: str) -> bool:
    return _state(name).get("enabled", False)


def _write_state(name: str, **changes: Any) -> None:
    _ensure_root()
    directory = package_dir(name)
    ensure_private_directory(directory)
    _write_json(directory / "state.json", {**_state(name), **changes})


def write_definition(definition: dict[str, Any]) -> str:
    """Write the connection fields to the native file; the description stays Avibe-side."""

    source = native.write(definition)
    _write_state(definition["name"], description=definition.get("description") or "", source=source)
    return source


def write_enabled(name: str, enabled: bool) -> None:
    _write_state(name, enabled=enabled)


def remember_source(name: str, source: str) -> None:
    _write_state(name, source=source)


def remove_package(name: str) -> None:
    directory = package_dir(name)
    removed = native.remove(name)
    if directory.exists():
        shutil.rmtree(directory)
    elif not removed:
        raise _not_found(name)


def list_names() -> list[str]:
    return sorted({*_native_servers(), *_generated_names()})


def _generated_names() -> list[str]:
    """Package directories that still have a published revision."""

    root = mcp_root()
    if not root.is_dir():
        return []
    names: list[str] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            validate_name(entry.name)
        except McpHubError:
            continue
        if (entry / "current.json").is_file():
            names.append(entry.name)
    return names


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


def _current_revision(name: str) -> Optional[str]:
    path = package_dir(name) / "current.json"
    try:
        value = _read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise McpHubError("invalid_package", f"{name}: current.json is unreadable: {exc}") from None
    revision = value.get("revision") if isinstance(value, dict) else None
    if not isinstance(revision, str) or not revision or "/" in revision or revision.startswith("."):
        raise McpHubError("invalid_package", f"{name}: current.json has an unsupported shape.")
    return revision


def _load_revision(name: str, revision: str) -> dict[str, Any]:
    directory = package_dir(name) / "revisions" / revision
    manifest = _read_json(directory / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("formatVersion") != FORMAT_VERSION:
        raise McpHubError(
            "needs_regeneration",
            f"{name}: the published package was written by another Avibe version.",
            hint=f"vibe mcp regenerate {name}",
        )
    published = {
        "revision": revision,
        "manifest": manifest,
        "definition": _read_json(directory / "definition.json"),
        "tools": _read_json(directory / "tools.json"),
        "commands": _read_json(directory / "commands.json"),
    }
    if not _well_formed(published):
        raise McpHubError("invalid_package", f"{name}: revision {revision} has an unsupported shape.")
    return published


_FLAG_FIELDS = {"flag": str, "key": str, "kind": str, "required": bool, "description": str}


def _well_formed(published: dict[str, Any]) -> bool:
    """The fields every reader dereferences, so a bad revision isolates its package."""

    manifest, definition = published["manifest"], published["definition"]
    tools, commands = published["tools"], published["commands"]
    if not isinstance(manifest.get("serverInfo", {}), dict) or not isinstance(definition, dict):
        return False
    transport = definition.get("transport")
    if not isinstance(transport, dict):
        return False
    if transport.get("type") == "stdio":
        if not isinstance(transport.get("command"), str) or not isinstance(transport.get("args", []), list):
            return False
    elif transport.get("type") != "streamable_http" or not isinstance(transport.get("url"), str):
        return False
    if not isinstance(tools, list) or not isinstance(commands, list) or len(tools) != len(commands):
        return False
    for tool, command in zip(tools, commands):
        if not isinstance(tool, dict) or not isinstance(command, dict) or not isinstance(tool.get("name"), str):
            return False
        if command.get("name") != tool["name"] or not isinstance(command.get("validated"), bool):
            return False
        if not isinstance(command.get("description"), str) or not all(
            isinstance(command.get(field), list) and all(isinstance(key, str) for key in command[field])
            for field in ("jsonOnlyKeys", "requiredJsonOnly")
        ):
            return False
        flags = command.get("flags")
        if not isinstance(flags, list) or not all(
            isinstance(flag, dict) and all(isinstance(flag.get(k), t) for k, t in _FLAG_FIELDS.items())
            for flag in flags
        ):
            return False
    return True


def read_published(name: str) -> Optional[dict[str, Any]]:
    """The active revision as one consistent snapshot, or ``None`` when never generated.

    A file missing mid-read means a concurrent publish pruned the revision we
    pointed at, so ``current.json`` is reread once.
    """

    for attempt in range(2):
        revision = _current_revision(name)
        if revision is None:
            return None
        try:
            return _load_revision(name, revision)
        except FileNotFoundError:
            if attempt:
                raise McpHubError("invalid_package", f"{name}: revision {revision} is incomplete.") from None
        except ValueError as exc:
            raise McpHubError("invalid_package", f"{name}: revision {revision} is unreadable: {exc}") from None
    return None


def new_revision_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def publish(name: str, *, definition: dict[str, Any], manifest: dict[str, Any], tools: list, commands: list) -> str:
    """Write a full revision, then switch ``current.json``; keep current + previous."""

    _ensure_root()
    directory = package_dir(name)
    ensure_private_directory(directory)
    previous = _current_revision(name)
    revision = new_revision_id()
    target = directory / "revisions" / revision
    ensure_private_directory(directory / "revisions")
    ensure_private_directory(target)
    _write_json(target / "definition.json", definition)
    _write_json(target / "tools.json", tools)
    _write_json(target / "commands.json", commands)
    _write_json(target / "manifest.json", {"formatVersion": FORMAT_VERSION, **manifest})
    _write_json(directory / "current.json", {"revision": revision})
    keep = {revision, previous}
    for entry in (directory / "revisions").iterdir():
        if entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)
    return revision


# ---------------------------------------------------------------------------
# Summaries for lists, the prompt index and the UI
# ---------------------------------------------------------------------------


def _comparable(definition: dict[str, Any], *, without_cwd: bool) -> dict[str, Any]:
    transport = dict(definition.get("transport") or {})
    if without_cwd:
        transport.pop("cwd", None)
    return {**definition, "transport": transport}


def summarize(name: str, servers: Optional[dict[str, native.NativeServer]] = None) -> dict[str, Any]:
    """One package's status. Bad files disable only this package and are not rewritten."""

    summary: dict[str, Any] = {"name": name, "enabled": False, "status": "error", "revision": None}
    try:
        servers = _native_servers() if servers is None else servers
        native_server = servers.get(name)
        source = native_server.source if native_server is not None else _state(name).get("source")
        if native_server is not None:
            summary["sourcePath"] = str(native_server.path)
            summary["hasCwd"] = native_server.has_cwd or native_server.source == native.CODEX
        if source in native.SOURCES:
            summary["source"] = source
        published = read_published(name)
        if native_server is None:
            if published is None:
                raise _not_found(name)
            definition = published["definition"]
            summary["nativeDeleted"] = True
        else:
            definition = read_definition(name, servers)
        enabled = read_enabled(name)
        summary["description"] = (definition.get("description") if isinstance(definition, dict) else "") or ""
        if isinstance(definition, dict) and isinstance(definition.get("transport"), dict):
            summary["transport"] = definition["transport"]["type"]
    except McpHubError as exc:
        if exc.type == "not_found":
            raise
        if exc.type != "needs_regeneration":
            logger.warning("MCP package %s disabled: %s", name, exc.message)
            summary["error"] = exc.payload()
            return summary
        summary.update(status="needs_regeneration", enabled=False, error=exc.payload())
        return summary
    if published is None:
        summary.update(status="draft", enabled=False)
        return summary
    manifest = published["manifest"]
    top_has_cwd = "cwd" in (definition.get("transport") or {})
    summary.update(
        status="generated",
        enabled=enabled,
        revision=published["revision"],
        description=manifest.get("description") or summary.get("description") or "",
        generatedAt=manifest.get("generatedAt"),
        serverVersion=(manifest.get("serverInfo") or {}).get("version"),
        toolCount=len(published["commands"]),
        pendingChanges=native_server is not None
        and _comparable(definition, without_cwd=not top_has_cwd)
        != _comparable(published["definition"], without_cwd=not top_has_cwd),
    )
    if manifest.get("diagnostics"):
        summary["diagnostics"] = manifest["diagnostics"]
    return summary


def list_packages() -> list[dict[str, Any]]:
    servers = _native_servers()
    hidden = native.disabled_names()
    names = set(servers)
    names.update(name for name in _generated_names() if name not in hidden)
    return [summarize(name, servers) for name in sorted(names)]


def enabled_generated() -> list[dict[str, Any]]:
    return [item for item in list_packages() if item["status"] == "generated" and item["enabled"]]


def default_cwd() -> str:
    """The sanitized Agent default directory frozen into stdio snapshots without a cwd."""

    try:
        from config.v2_config import V2Config

        candidate = V2Config.load().runtime.default_cwd
    except Exception:
        candidate = None
    if isinstance(candidate, str) and candidate.strip():
        resolved = os.path.abspath(os.path.expanduser(candidate.strip()))
        if os.path.isdir(resolved):
            return resolved
    return str(Path.home())
