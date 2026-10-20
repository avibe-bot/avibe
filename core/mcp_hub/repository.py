"""On-disk MCP packages under ``<avibe-home>/mcp/<name>/``.

``definition.json`` and ``state.json`` are the only authorities. Revisions are
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
from core.mcp_hub.definition import SCHEMA_VERSION, validate_name
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


def read_definition(name: str) -> dict[str, Any]:
    path = package_dir(name) / "definition.json"
    try:
        value = _read_json(path)
    except FileNotFoundError:
        raise _not_found(name) from None
    except (OSError, ValueError) as exc:
        raise McpHubError("invalid_package", f"{name}: definition.json is unreadable: {exc}") from None
    if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA_VERSION or value.get("name") != name:
        raise McpHubError("invalid_package", f"{name}: definition.json has an unsupported shape or version.")
    return value


def read_enabled(name: str) -> bool:
    path = package_dir(name) / "state.json"
    try:
        value = _read_json(path)
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise McpHubError("invalid_package", f"{name}: state.json is unreadable: {exc}") from None
    if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
        raise McpHubError("invalid_package", f"{name}: state.json has an unsupported shape.")
    return value["enabled"]


def write_definition(definition: dict[str, Any]) -> None:
    _ensure_root()
    directory = package_dir(definition["name"])
    ensure_private_directory(directory)
    _write_json(directory / "definition.json", definition)


def write_enabled(name: str, enabled: bool) -> None:
    _write_json(package_dir(name) / "state.json", {"enabled": enabled})


def remove_package(name: str) -> None:
    directory = package_dir(name)
    if not directory.exists():
        raise _not_found(name)
    shutil.rmtree(directory)


def list_names() -> list[str]:
    root = mcp_root()
    try:
        entries = sorted(entry.name for entry in root.iterdir() if entry.is_dir())
    except FileNotFoundError:
        return []
    names = []
    for entry in entries:
        try:
            names.append(validate_name(entry))
        except McpHubError:
            continue
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
    return {
        "revision": revision,
        "manifest": manifest,
        "definition": _read_json(directory / "definition.json"),
        "tools": _read_json(directory / "tools.json"),
        "commands": _read_json(directory / "commands.json"),
    }


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

    directory = package_dir(name)
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


def summarize(name: str) -> dict[str, Any]:
    """One package's status. Bad files disable only this package and are not rewritten."""

    summary: dict[str, Any] = {"name": name, "enabled": False, "status": "error", "revision": None}
    try:
        definition = read_definition(name)
        enabled = read_enabled(name)
        summary["description"] = definition.get("description") or ""
        summary["transport"] = definition["transport"]["type"]
        published = read_published(name)
    except McpHubError as exc:
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
        description=manifest.get("description") or "",
        generatedAt=manifest.get("generatedAt"),
        serverVersion=(manifest.get("serverInfo") or {}).get("version"),
        toolCount=len(published["commands"]),
        pendingChanges=_comparable(definition, without_cwd=not top_has_cwd)
        != _comparable(published["definition"], without_cwd=not top_has_cwd),
    )
    if manifest.get("diagnostics"):
        summary["diagnostics"] = manifest["diagnostics"]
    return summary


def list_packages() -> list[dict[str, Any]]:
    return [summarize(name) for name in list_names()]


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
