"""On-disk MCP packages under ``<avibe-home>/mcp/<name>/``.

Avibe-added servers store ``definition.json`` here. Native Claude/Codex/OpenCode
entries are read-only sources. A published revision (``current.json``) is what
makes a server registered and listable. Leftover ``state.json`` ``enabled``
flags from the MVP lifecycle are ignored. Writers are serialized per package by
the owner (``core/mcp_hub/runtime.py``).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import threading
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
AVIBE = "avibe"
_HIDDEN_FILE = ".hidden.json"
_HIDDEN_LOCK = threading.Lock()


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
    return McpHubError("not_found", f"MCP server {name!r} does not exist.", hint="vibe mcp --list")


# ---------------------------------------------------------------------------
# Authorities
# ---------------------------------------------------------------------------


def _state(name: str) -> dict[str, Any]:
    """Leftover MVP ``state.json``. Unreadable files are ignored, not fatal."""

    path = package_dir(name) / "state.json"
    try:
        value = _read_json(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _hidden_path() -> Path:
    return mcp_root() / _HIDDEN_FILE


def hidden_names() -> set[str]:
    try:
        value = _read_json(_hidden_path())
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        logger.warning("MCP hidden.json is unreadable; treating as empty")
        return set()
    names = value.get("names") if isinstance(value, dict) else None
    if not isinstance(names, list):
        return set()
    return {str(item) for item in names if isinstance(item, str) and item}


def hide_name(name: str) -> None:
    with _HIDDEN_LOCK:
        _ensure_root()
        names = sorted(hidden_names() | {validate_name(name)})
        _write_json(_hidden_path(), {"names": names})


def unhide_name(name: str) -> None:
    with _HIDDEN_LOCK:
        names = hidden_names()
        names.discard(name)
        _ensure_root()
        _write_json(_hidden_path(), {"names": sorted(names)})


def _avibe_definition_path(name: str) -> Path:
    return package_dir(name) / "definition.json"


def read_avibe_definition(name: str) -> Optional[dict[str, Any]]:
    path = _avibe_definition_path(name)
    try:
        value = _read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise McpHubError("invalid_package", f"{name}: definition.json is unreadable: {exc}") from None
    if not isinstance(value, dict) or not isinstance(value.get("transport"), dict):
        raise McpHubError("invalid_package", f"{name}: definition.json has an unsupported shape.")
    return value


def write_avibe_definition(definition: dict[str, Any]) -> None:
    name = validate_name(definition["name"])
    unhide_name(name)
    directory = package_dir(name)
    ensure_private_directory(directory)
    _write_json(directory / "definition.json", definition)


def rollback_avibe_definition(name: str, previous: Optional[dict[str, Any]], *, hidden: bool = False) -> None:
    """Undo an add that failed to refresh: restore the prior Avibe file or delete it."""

    validate_name(name)
    path = _avibe_definition_path(name)
    if previous is not None:
        write_avibe_definition(previous)
        if hidden:
            hide_name(name)
        return
    if path.is_file():
        path.unlink()
    directory = package_dir(name)
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
    if hidden:
        hide_name(name)


def unlist(name: str) -> None:
    """Drop ``current.json`` so a failed refresh is not listed, keeping revision files."""

    validate_name(name)
    path = package_dir(name) / "current.json"
    if path.is_file():
        path.unlink()


def source_of(name: str) -> Optional[str]:
    if read_avibe_definition(name) is not None:
        return AVIBE
    native_server = native.read_all().get(name)
    return native_server.source if native_server is not None else None


def read_definition(name: str, servers: Optional[dict[str, native.NativeServer]] = None) -> dict[str, Any]:
    """Avibe store wins; otherwise the canonical native file."""

    avibe = read_avibe_definition(name)
    if avibe is not None:
        return avibe
    server = (native.read_all() if servers is None else servers).get(name)
    if server is None:
        raise _not_found(name)
    if server.error is not None:
        raise server.error
    return server.definition


def write_definition(definition: dict[str, Any]) -> str:
    """Persist an Avibe-owned definition; never writes native files."""

    write_avibe_definition(definition)
    return AVIBE


def remove_package(name: str) -> str:
    """Avibe source: delete artifacts. Native source: hide in Avibe only."""

    validate_name(name)
    if read_avibe_definition(name) is not None:
        directory = package_dir(name)
        if directory.exists():
            shutil.rmtree(directory)
        return AVIBE
    if name in native.read_all() or name in native.disabled_names():
        hide_name(name)
        directory = package_dir(name)
        if directory.exists():
            shutil.rmtree(directory)
        return "native"
    directory = package_dir(name)
    if directory.exists():
        shutil.rmtree(directory)
        return AVIBE
    raise _not_found(name)


def delete_artifacts(name: str) -> None:
    directory = package_dir(name)
    if directory.exists() and read_avibe_definition(name) is None:
        shutil.rmtree(directory)


def list_names() -> list[str]:
    return sorted(item["name"] for item in list_packages())


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
            hint=f"vibe mcp {name} --refresh",
        )
    published = {
        "revision": revision,
        "manifest": manifest,
        "definition": _read_json(directory / "definition.json"),
        "tools": _read_json(directory / "tools.json"),
        "commands": _read_json(directory / "commands.json"),
    }
    if not isinstance(published["definition"], dict) or not isinstance(published["tools"], list) or not isinstance(published["commands"], list):
        raise McpHubError("invalid_package", f"{name}: revision {revision} has an unsupported shape.")
    return published


def read_published(name: str) -> Optional[dict[str, Any]]:
    """The active revision as one consistent snapshot, or ``None`` when never generated.

    An unreadable or unknown-format snapshot omits the package.
    """

    revision = _current_revision(name)
    if revision is None:
        return None
    try:
        return _load_revision(name, revision)
    except FileNotFoundError:
        raise McpHubError("invalid_package", f"{name}: revision {revision} is incomplete.") from None
    except (OSError, ValueError) as exc:
        raise McpHubError("invalid_package", f"{name}: revision {revision} is unreadable: {exc}") from None


def new_revision_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def publish(name: str, *, definition: dict[str, Any], manifest: dict[str, Any], tools: list, commands: list) -> str:
    """Write one published snapshot, then switch ``current.json``."""

    _ensure_root()
    directory = package_dir(name)
    ensure_private_directory(directory)
    revision = new_revision_id()
    target = directory / "revisions" / revision
    ensure_private_directory(directory / "revisions")
    ensure_private_directory(target)
    _write_json(target / "definition.json", definition)
    _write_json(target / "tools.json", tools)
    _write_json(target / "commands.json", commands)
    _write_json(target / "manifest.json", {"formatVersion": FORMAT_VERSION, **manifest})
    _write_json(directory / "current.json", {"revision": revision})
    for entry in (directory / "revisions").iterdir():
        if entry.name != revision:
            shutil.rmtree(entry, ignore_errors=True)
    return revision


# ---------------------------------------------------------------------------
# Summaries for lists, the prompt index and the UI
# ---------------------------------------------------------------------------


def summarize(name: str, servers: Optional[dict[str, native.NativeServer]] = None) -> dict[str, Any]:
    """Registered package only. Missing or unpublished names raise ``not_found``."""

    published = read_published(name)
    if published is None:
        raise _not_found(name)
    definition = published["definition"]
    transport = definition.get("transport") if isinstance(definition, dict) else {}
    native_server = (native.read_all() if servers is None else servers).get(name)
    try:
        avibe = read_avibe_definition(name)
    except McpHubError:
        avibe = None
    if avibe is not None:
        source = AVIBE
        source_path = str(_avibe_definition_path(name))
    elif native_server is not None:
        source = native_server.source
        source_path = str(native_server.path)
    else:
        leftover = _state(name).get("source")
        source = leftover if leftover in (*native.SOURCES, AVIBE) else AVIBE
        source_path = str(_avibe_definition_path(name)) if source == AVIBE else ""
    manifest = published["manifest"]
    command = ""
    url = ""
    if isinstance(transport, dict):
        if transport.get("type") == "stdio":
            command = " ".join([transport.get("command") or "", *(transport.get("args") or [])]).strip()
        else:
            url = str(transport.get("url") or "")
    return {
        "name": name,
        "source": source,
        "sourcePath": source_path,
        "description": manifest.get("description") or "",
        "transport": transport.get("type") if isinstance(transport, dict) else None,
        "command": command,
        "url": url,
        "revision": published["revision"],
        "generatedAt": manifest.get("generatedAt"),
        "serverVersion": (manifest.get("serverInfo") or {}).get("version"),
        "toolCount": len(published["commands"]),
        "tools": [
            {"name": tool.get("name"), "description": tool.get("description") or ""}
            for tool in published["tools"]
            if isinstance(tool, dict)
        ],
        "diagnostics": manifest.get("diagnostics") or [],
    }


def _candidate_names(servers: dict[str, native.NativeServer]) -> set[str]:
    avibe_owned: set[str] = set()
    names: set[str] = set()
    root = mcp_root()
    if root.is_dir():
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            try:
                validate_name(entry.name)
            except McpHubError:
                continue
            if (entry / "definition.json").is_file():
                names.add(entry.name)
                avibe_owned.add(entry.name)
    for name in servers:
        try:
            validate_name(name)
        except McpHubError:
            continue
        names.add(name)
    hidden = hidden_names() | (native.disabled_names() - avibe_owned)
    return {name for name in names if name not in hidden}


def list_packages() -> list[dict[str, Any]]:
    """Registered servers only (published revision present)."""

    servers = native.read_all()
    listed: list[dict[str, Any]] = []
    for name in sorted(_candidate_names(servers)):
        try:
            if name not in servers and read_avibe_definition(name) is None:
                continue
            listed.append(summarize(name, servers))
        except McpHubError:
            continue
    return listed





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
