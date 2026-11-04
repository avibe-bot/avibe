"""Skills-aligned MCP visibility and repository-owned project definitions.

A project's ``.mcp.json`` sits at the same root managed Skills use. Avibe keeps
per-server secrets, approval, OAuth and published revisions in its own cache
under ``<avibe-home>/project_mcp/<key>/<name>/``. Reads never write or delete
that cache; only an explicit Owner remove deletes it.

All functions here do filesystem/DB work; async callers use a worker thread.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from config import paths
from config.atomic_io import write_atomic
from core.managed_skills import managed_skill_project_root
from core.mcp_hub import native, repository
from core.mcp_hub.definition import (
    _ENV_KEY,
    _HEADER_NAME,
    _string_map,
    definition_from_provider,
    provider_entry,
    validate_name,
)
from core.mcp_hub.errors import McpHubError
from core.private_fs import ensure_private_directory

logger = logging.getLogger(__name__)

GLOBAL = "global"
PROJECT_FILE = ".mcp.json"
_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_WRITTEN_FIELDS = ("type", "command", "args", "url", "cwd", "oauth")
_TRANSPORT_FIELDS = ("command", "args", "url")
_KIND_FIELDS = ("type", "cwd", "env", "headers")
BLOCKED_STATES = {"pending_approval", "needs_config"}
# Stand-ins for missing ``${VAR}`` values so the parser still checks the entry's structure.
# They only feed that check: a definition with a missing variable is never returned.
_STAND_IN = "0"
_URL_STAND_IN = "https://placeholder.invalid/"
# Package locks are per server; this one covers the shared ``.mcp.json`` read-modify-replace.
_DOCUMENT_LOCK = threading.Lock()


@dataclass(frozen=True)
class Scope:
    key: str = GLOBAL
    project_root: Optional[Path] = None

    @property
    def root(self) -> Path:
        if self.key == GLOBAL:
            return repository.mcp_root()
        return paths.get_vibe_remote_dir() / "project_mcp" / self.key.removeprefix("project:")


@dataclass(frozen=True)
class ScopedName:
    """Runtime key for locks, connections and OAuth: same name, different scope, different key."""

    scope: Scope
    name: str

    def __str__(self) -> str:
        return self.name

    @property
    def root(self) -> Path:
        return self.scope.root

    @property
    def project_root(self) -> Optional[Path]:
        return self.scope.project_root


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


def _directory_key(root: Path) -> str:
    project = None
    if paths.get_sqlite_state_path().is_file():
        from storage.db import get_cached_sqlite_engine
        from storage.projects_service import _find_project_by_workdir
        from vibe.authorization import instance_owner_context

        with get_cached_sqlite_engine().connect() as conn:
            project = _find_project_by_workdir(conn, instance_owner_context(), str(root))
    if project:
        return project["native_id"]
    return "dir-" + hashlib.sha256(str(root).encode()).hexdigest()[:12]


def project_scope(
    working_directory: Union[str, Path, None],
    project_base: Union[str, Path, None] = None,
    *,
    require_config: bool = True,
) -> Optional[Scope]:
    if not working_directory:
        return None
    root = managed_skill_project_root(working_directory, project_base)
    if require_config and not (root / PROJECT_FILE).exists():
        return None
    return Scope("project:" + _directory_key(root), root)


def scopes(working_directory: Union[str, Path, None] = None, project_base: Union[str, Path, None] = None) -> list[Scope]:
    project = project_scope(working_directory, project_base)
    return [Scope(), project] if project else [Scope()]


def _project_workdir(project_id: str) -> str:
    from storage.db import get_cached_sqlite_engine
    from storage.projects_service import get_project_workdir
    from vibe.authorization import instance_owner_context

    try:
        with get_cached_sqlite_engine().connect() as conn:
            return get_project_workdir(conn, project_id, authorization_context=instance_owner_context())
    except LookupError:
        raise McpHubError("not_found", "Project not found.") from None


def request_scopes(payload: dict[str, Any]) -> list[Scope]:
    """``global`` forces the global scope; a Web ``project`` id selects that folder."""

    if payload.get("global") is True:
        return [Scope()]
    if payload.get("project"):
        workdir = _project_workdir(str(payload["project"]))
        if not workdir:
            raise McpHubError("usage", "This project has no folder, so it has no project MCP servers.")
        return [Scope(), project_scope(workdir, workdir, require_config=False)]
    return scopes(payload.get("workingDirectory"), payload.get("projectBase"))


# ---------------------------------------------------------------------------
# Documents and cache
# ---------------------------------------------------------------------------


def _project_file(scope: Scope) -> Path:
    return scope.project_root / PROJECT_FILE


def _document(scope: Scope) -> tuple[Optional[str], dict[str, Any]]:
    """Raw text (for compare-and-replace) and parsed document; a missing file is empty."""

    try:
        text = _project_file(scope).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, {"mcpServers": {}}
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ValueError("invalid MCP project document")
    return text, data


def _raw_entry(key: ScopedName) -> dict[str, Any]:
    if key.project_root is None:  # a global server's template is its Avibe definition.json
        definition = repository.read_avibe_definition(key.name)
        if definition is None:
            raise McpHubError("usage", "Configure applies only to project servers and Avibe-added servers.")
        return provider_entry(definition)
    try:
        _, document = _document(key.scope)
    except (OSError, ValueError) as exc:
        raise McpHubError("invalid_definition", f"{_project_file(key.scope)} is unreadable: {exc}") from None
    raw = document.get("mcpServers", {}).get(key.name)
    if not isinstance(raw, dict):
        raise repository._not_found(key.name)
    validate_name(key.name)
    return raw


def _cached(key: ScopedName, file: str) -> dict[str, Any]:
    try:
        value = repository._read_json(repository.package_dir(key) / file)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise McpHubError("invalid_package", f"{key}: {file} is unreadable: {exc}") from None
    if not isinstance(value, dict):
        raise McpHubError("invalid_package", f"{key}: {file} has an unsupported shape.")
    return value


def _write_cache(key: ScopedName, file: str, value: dict[str, Any]) -> None:
    directory = repository.package_dir(key)
    if key.project_root is not None:
        ensure_private_directory(key.root.parent)
    ensure_private_directory(key.root)
    ensure_private_directory(directory)
    repository._write_json(directory / file, value)


def _check_secrets(secrets: Any) -> dict[str, dict[str, str]]:
    """The normalized secret maps, by the parser's own key and value rules.

    An absent field is omitted; a supplied one, including ``null``, must be a string map.
    """

    if not isinstance(secrets, dict) or set(secrets) - {"env", "headers"}:
        raise McpHubError("usage", "Secrets must contain env and/or headers string maps.", usage=True)
    checked = {}
    for field, pattern in (("env", _ENV_KEY), ("headers", _HEADER_NAME)):
        if field not in secrets:
            continue
        if not isinstance(secrets[field], dict):
            raise McpHubError("invalid_definition", f"{field} must be an object of string values.", field=field)
        checked[field] = _string_map(secrets[field], field, pattern)
    return checked


def _secrets(key: ScopedName) -> dict[str, dict[str, str]]:
    """Persisted secrets pass the same check as input; a malformed file is ``invalid_package``."""

    try:
        return _check_secrets(_cached(key, "secrets.json"))
    except McpHubError as exc:
        if exc.type == "invalid_package":
            raise
        raise McpHubError("invalid_package", f"{key}: secrets.json is invalid: {exc.message}") from None


def fingerprint(raw: dict[str, Any]) -> str:
    """Approval binds to the raw entry, so secret rotation keeps it and edits drop it."""

    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Effective servers
# ---------------------------------------------------------------------------


def state_note(name: str, state: str, missing: list[str]) -> tuple[str, str]:
    """What blocks a project server, and the Owner command that unblocks it."""

    if state == "pending_approval":
        return "pending approval", f"run vibe mcp {name} --approve"
    return f"needs config: {', '.join(missing)}", f"run vibe mcp {name} --configure '{{\"env\": {{...}}}}'"


def state_error(name: str, state: str, missing: list[str]) -> McpHubError:
    note, hint = state_note(name, state, missing)
    return McpHubError(state, f"MCP server {name}: {note}.", hint=hint)


@dataclass
class ScopedServer:
    key: ScopedName
    raw: dict[str, Any]
    definition: Optional[dict[str, Any]]
    state: str
    missing: list[str]
    placeholders: list[str]

    def require_definition(self) -> dict[str, Any]:
        if self.state in BLOCKED_STATES:
            raise state_error(str(self.key), self.state, self.missing)
        return self.definition

    def _published_state(self, published: Optional[dict[str, Any]]) -> str:
        if self.state != "ok":
            return self.state
        if published is None or published["definition"] != self.definition:
            return "not_generated"
        if published["manifest"].get("auth") == "needs_login":
            return "needs_login"
        return "ok"

    def summary(self) -> dict[str, Any]:
        """Safe metadata: raw URL/command only, never substituted secrets."""

        published = repository.read_published(self.key)
        state = self._published_state(published)
        command = " ".join([self.raw.get("command", ""), *self.raw.get("args", [])]).strip()
        row = {
            "name": str(self.key),
            "scope": self.key.scope.key,
            "state": state,
            "source": "project",
            "sourcePath": str(_project_file(self.key.scope)),
            "missingVariables": self.missing,
            "placeholders": self.placeholders,
            "url": self.raw.get("url", ""),
            "command": command,
            "transport": "http" if "url" in self.raw else "stdio",
            "description": "",
            "auth": "needs_login" if state == "needs_login" else "none",
            "toolCount": 0,
            "tools": [],
            "revision": None,
            "diagnostics": [],
        }
        if published and state in {"ok", "needs_login"}:
            tools = [{"name": tool["name"], "description": tool.get("description", "")} for tool in published["tools"]]
            row["description"] = published["manifest"].get("description", "")
            row["revision"] = published["revision"]
            row["toolCount"] = len(published["commands"])
            row["tools"] = tools
        return row


def _reject_command_placeholders(name: str, raw: dict[str, Any]) -> None:
    """Placeholders live only in url, env values and header values (D4)."""

    args = raw.get("args")
    values = [raw.get("command"), *(args if isinstance(args, list) else [])]
    if any(isinstance(value, str) and _TOKEN.search(value) for value in values):
        message = f"{name}: ${{VAR}} is allowed only in url, env and headers."
        raise McpHubError("invalid_definition", message, field="command")


def _merge_headers(declared: dict[str, str], cached: dict[str, str]) -> dict[str, str]:
    """Cached header values override declared ones case-insensitively."""

    combined = {key.lower(): (key, value) for key, value in declared.items()}
    combined.update({key.lower(): (key, value) for key, value in cached.items()})
    return dict(combined.values())


def _effective(key: ScopedName, raw: dict[str, Any], secrets: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """The structurally validated definition, missing names, and placeholders.

    Callers must not execute the definition while variables are missing.

    The single structural check of a project entry: the parser always runs, with a stand-in for
    each missing variable, so ``needs_config`` never hides a malformed entry.
    """

    _reject_command_placeholders(key.name, raw)
    variables = secrets.get("env", {})
    headers = secrets.get("headers", {})
    missing: set[str] = set()
    placeholders: set[str] = set()

    def replace(match: re.Match) -> str:
        placeholders.add(match[1])
        if match[1] not in variables:
            missing.add(match[1])
        return variables.get(match[1], _STAND_IN)

    def substitute(value: Any) -> Any:
        return _TOKEN.sub(replace, value) if isinstance(value, str) else value

    merged = dict(raw)
    if "url" in raw:
        merged["url"] = substitute(raw["url"])
        if missing:  # a URL with a missing part is checked once it is complete
            merged["url"] = _URL_STAND_IN
    for field in ("env", "headers"):
        if field not in raw:
            continue
        if not isinstance(raw[field], dict):
            raise McpHubError("invalid_definition", f"{key.name}: {field} must be an object.", field=field)
        merged[field] = {k: substitute(v) for k, v in raw[field].items()}
    if "command" in raw:
        if not isinstance(raw.get("cwd", ""), str):
            raise McpHubError("invalid_definition", f"{key.name}: cwd must be a string.", field="cwd")
        merged["env"] = {**merged.get("env", {}), **variables}
        if key.project_root is not None:  # a global cwd stays as written and must be absolute
            merged["cwd"] = native._resolve_cwd(_project_file(key.scope), raw.get("cwd") or str(key.project_root))
    else:
        merged["headers"] = _merge_headers(merged.get("headers", {}), headers)
    definition = definition_from_provider(key.name, merged)
    return definition, sorted(missing), sorted(placeholders)


def add_definition(name: str, raw: object, *, project: bool) -> dict[str, Any]:
    """The one add validator: check with the loader's stand-ins, then restore unresolved input values.

    This is parsing only: approval and the real project's scope are owned by the
    controller. A project's temporary scope supplies a cwd for structural validation.
    """

    validate_name(name)
    if not isinstance(raw, dict):
        return definition_from_provider(name, raw)
    validation = Scope("project:validation", Path.cwd()) if project else Scope()
    definition, _, _ = _effective(ScopedName(validation, name), raw, {})
    transport = definition["transport"]
    for field in ("url", "env", "headers", "cwd"):
        if field in raw:
            transport[field] = copy.deepcopy(raw[field])
    if "cwd" not in raw:
        transport.pop("cwd", None)
    return definition


def project_add_definition(name: str, raw: object) -> dict[str, Any]:
    return add_definition(name, raw, project=True)


def global_add_definition(name: str, raw: object) -> dict[str, Any]:
    return add_definition(name, raw, project=False)


# ---------------------------------------------------------------------------
# Global (Avibe-owned) placeholder servers
# ---------------------------------------------------------------------------


def global_key(name: object) -> ScopedName:
    return ScopedName(Scope(), str(name))


def _template_values(definition: dict[str, Any]) -> list[str]:
    transport = definition.get("transport") or {}
    values = [transport.get("url")]
    for field in ("env", "headers"):
        mapping = transport.get(field)
        if isinstance(mapping, dict):
            values.extend(mapping.values())
    return [value for value in values if isinstance(value, str)]


def uses_placeholders(definition: dict[str, Any]) -> bool:
    """Whether a definition carries ``${VAR}`` templates."""

    return any(_TOKEN.search(value) for value in _template_values(definition))


def global_state(name: object, definition: dict[str, Any]) -> tuple[Optional[dict[str, Any]], list[str], list[str]]:
    """The privately resolved definition (``None`` while variables are missing), missing names, placeholders."""

    key = global_key(name)
    resolved, missing, placeholders = _effective(key, provider_entry(definition), _secrets(key))
    return (None if missing else resolved), missing, placeholders


def resolve_global(name: object, definition: dict[str, Any]) -> dict[str, Any]:
    """The definition execution uses. Templates resolve in memory only; nothing resolved is stored."""

    if not uses_placeholders(definition) and read_global_secrets(name) is None:
        return definition  # released and native definitions run exactly as stored
    resolved, missing, _ = global_state(name, definition)
    if resolved is None:
        raise state_error(str(name), "needs_config", missing)
    return resolved


def split_global(definition: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """``(template, private maps, missing)`` for a validated Avibe-owned global add.

    The project contract of :func:`write_server`, per key: supplied templates go to
    ``definition.json``, supplied concrete values to ``secrets.json``; unsupplied keys
    of the stored definition (same transport kind) and of the sidecar are kept.
    """

    key = global_key(definition["name"])
    template = copy.deepcopy(definition)
    transport = template["transport"]
    try:
        previous = repository.read_avibe_definition(key.name)
    except McpHubError:
        previous = None
    try:
        cache = _secrets(key)
    except McpHubError:
        cache = {}
    old = (previous or {}).get("transport") or {}
    written = {}
    if old.get("type") == transport["type"]:
        written = {field: dict(old[field]) for field in ("env", "headers") if old.get(field)}
    _split_values(written, cache, {field: transport[field] for field in ("env", "headers") if field in transport})
    for field in ("env", "headers"):
        if field in transport:
            transport[field] = written.get(field, {})
    cache = _check_secrets(cache)
    _, missing, _ = _effective(key, provider_entry(template), cache)
    return template, cache, missing


def read_global_secrets(name: object) -> Optional[bytes]:
    path = repository.package_dir(global_key(name)) / "secrets.json"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def write_global_secrets(name: object, value: Union[dict[str, Any], bytes, None]) -> None:
    """Store (or, for ``None``, delete) a global server's private ``secrets.json`` sidecar.

    An empty map with no sidecar yet writes nothing.
    """

    key = global_key(name)
    path = repository.package_dir(key) / "secrets.json"
    if isinstance(value, dict) and not any(value.values()) and not path.exists():
        return
    if value is None:
        path.unlink(missing_ok=True)
    elif isinstance(value, bytes):
        ensure_private_directory(path.parent)
        write_atomic(path, value)
    else:
        _write_cache(key, "secrets.json", value)


def global_pending_row(name: str) -> Optional[dict[str, Any]]:
    """The management row of an unpublished Avibe server; never resolved values.

    ``needs_config`` while variables are missing, otherwise ``not_generated``
    (configured but not discovered yet): manageable, never callable.
    """

    try:
        definition = repository.read_avibe_definition(name)
    except McpHubError:
        return None
    if definition is None:
        return None
    try:
        _, missing, placeholders = global_state(name, definition)
    except McpHubError as exc:
        logger.warning("Skipping invalid MCP server %s: %s", name, exc)
        return None
    transport = definition["transport"]
    return {
        "name": name,
        "scope": GLOBAL,
        "state": "needs_config" if missing else "not_generated",
        "source": repository.AVIBE,
        "sourcePath": str(repository.package_dir(name) / "definition.json"),
        "missingVariables": missing,
        "placeholders": placeholders,
        "url": transport.get("url", ""),
        "command": " ".join([transport.get("command") or "", *(transport.get("args") or [])]).strip(),
        "transport": transport.get("type"),
        "description": "",
        "auth": "none",
        "toolCount": 0,
        "tools": [],
        "revision": None,
        "diagnostics": [],
    }


def load_server(key: ScopedName) -> ScopedServer:
    raw = _raw_entry(key)
    definition, missing, placeholders = _effective(key, raw, _secrets(key))
    if missing:
        definition = None
    approved = _cached(key, "approval.json").get("fingerprint") == fingerprint(raw)
    if not approved:
        state = "pending_approval"
    elif missing:
        state = "needs_config"
    else:
        state = "ok"
    return ScopedServer(key, raw, definition, state, missing, placeholders)


# ---------------------------------------------------------------------------
# Resolution and listing (reads never write or delete)
# ---------------------------------------------------------------------------


def _project_names(scope: Scope) -> dict[str, Any]:
    try:
        _, data = _document(scope)
    except (OSError, ValueError):
        logger.warning("MCP project config unreadable: %s", scope.project_root)
        return {}
    return data.get("mcpServers", {})


def resolve(selected: list[Scope], name: str) -> ScopedName:
    """Bounded name lookup: the caller's project entry shadows global; nothing else is read."""

    project = selected[-1]
    if project.project_root is not None and name in _project_names(project):
        return ScopedName(project, name)
    return ScopedName(Scope(), name)


def require_listed(key: ScopedName) -> None:
    """An entry the list skips as invalid is ``not_found`` for reads and calls."""

    if not repository.is_project(key):
        return
    try:
        load_server(key)
    except McpHubError as exc:
        logger.warning("Invalid project MCP server %s: %s", key, exc)
        raise repository._not_found(key.name) from None


def project_servers(scope: Scope) -> dict[str, Optional[ScopedServer]]:
    """Every entry of the project file; ``None`` marks an invalid entry that still shadows global."""

    result: dict[str, Optional[ScopedServer]] = {}
    for name in _project_names(scope):
        try:
            result[name] = load_server(ScopedName(scope, name))
        except McpHubError as exc:
            logger.warning("Skipping invalid project MCP server %s: %s", name, exc)
            result[name] = None
    return result


def global_row(row: dict[str, Any]) -> dict[str, Any]:
    """A published global row; a placeholder server whose variables went missing is ``needs_config``."""

    state = "needs_login" if row.get("auth") == "needs_login" else "ok"
    row = {**row, "scope": GLOBAL, "state": state}
    if row.get("source") != repository.AVIBE:
        return row
    try:
        definition = repository.read_avibe_definition(row["name"])
        if definition is not None and uses_placeholders(definition):
            _, missing, placeholders = global_state(row["name"], definition)
            row.update(missingVariables=missing, placeholders=placeholders)
            if missing:
                row["state"] = "needs_config"
    except McpHubError as exc:
        logger.warning("MCP server %s has an unreadable configuration: %s", row["name"], exc)
    return row


def _global_rows() -> dict[str, dict[str, Any]]:
    rows = {row["name"]: global_row(row) for row in repository.list_packages()}
    for name in repository._candidate_names({}):
        if name not in rows:
            pending = global_pending_row(name)
            if pending is not None:
                rows[name] = pending
    return rows


def summaries(selected: list[Scope]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for scope in selected:
        if scope.key == GLOBAL:
            rows.update(_global_rows())
            continue
        for name, server in project_servers(scope).items():
            rows.pop(name, None)
            if server is None:
                continue
            try:
                rows[name] = server.summary()
            except McpHubError as exc:
                logger.warning("Skipping unreadable project MCP package %s: %s", name, exc)
    return sorted(rows.values(), key=lambda row: row["name"])


# ---------------------------------------------------------------------------
# Owner writes (callers hold the package lock)
# ---------------------------------------------------------------------------


def approve(key: ScopedName) -> None:
    raw = _raw_entry(key)
    _reject_command_placeholders(key.name, raw)
    _write_cache(key, "approval.json", {"fingerprint": fingerprint(raw)})


def _merged_secrets(key: ScopedName, update: dict[str, Any]) -> dict[str, Any]:
    """A supplied map replaces that map; an omitted map is kept. A broken cache is replaced."""

    try:
        previous = _secrets(key)
    except McpHubError:
        previous = {}
    return {**previous, **update}


def prepare_configure(key: ScopedName, secrets: Any) -> dict[str, Any]:
    """The validated prospective ``secrets.json``; nothing is written."""

    raw = _raw_entry(key)
    prospective = _merged_secrets(key, _check_secrets(secrets))
    _effective(key, raw, prospective)
    return prospective


def configure(key: ScopedName, secrets: Any) -> None:
    _write_cache(key, "secrets.json", prepare_configure(key, secrets))


def _apply_entry(servers: dict[str, Any], key: ScopedName, entry: dict[str, Any], replace: bool) -> None:
    """Replace the transport; keep the file's env/headers, cwd, type and unknown keys.

    A transport change (command <-> url) also drops the fields of the old kind.
    """

    if key.name in servers and not replace:
        raise McpHubError("exists", f"MCP server {key.name} already exists.")
    old = servers.get(key.name)
    old = old if isinstance(old, dict) else {}
    kept = {k: v for k, v in old.items() if k not in _TRANSPORT_FIELDS}
    if ("url" in old) != ("url" in entry):
        for field in _KIND_FIELDS:
            kept.pop(field, None)
    kept.update({k: v for k, v in entry.items() if k in _WRITTEN_FIELDS})
    servers[key.name] = kept


def _write_document(path: Path, text: str, expected: Optional[str]) -> None:
    def unchanged() -> None:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != expected:
            message = f"{path} changed while Avibe was writing it; your edit was kept. Retry the command."
            raise McpHubError("config_changed", message)

    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    write_atomic(path, text, follow_symlinks=True, mode=mode, before_replace=unchanged)


def _render_document(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _edit_document_text(text: Optional[str], key: ScopedName, entry: Optional[dict[str, Any]], data: dict[str, Any]) -> Optional[str]:
    """The project file's next text: a member edit that keeps the file's formatting.

    The edit's parse must equal ``data`` (the document's expected next state);
    ``None`` otherwise (missing file or an unusual layout), and the caller
    falls back to a full rewrite.
    """

    if text is None:
        return None
    from vibe.opencode_config import parse_jsonc_object, remove_jsonc_member, upsert_jsonc_object_member

    try:
        if entry is None:
            next_text = remove_jsonc_member(text, "mcpServers", key.name)
        else:
            next_text = upsert_jsonc_object_member(text, "mcpServers", key.name, entry)
        if parse_jsonc_object(next_text) != data:
            return None
        return next_text
    except (KeyError, ValueError):
        return None


def _split_values(written: dict[str, Any], cache: dict[str, Any], entry: dict[str, Any]) -> None:
    """Route each supplied env/header value: a ``${VAR}`` template to ``written``, a concrete value to ``cache``.

    Each supplied key replaces its previous destination.
    """

    for field in ("env", "headers"):
        if field not in entry:
            continue
        declared = dict(written.get(field, {}))
        private = dict(cache.get(field, {}))
        for name, value in entry[field].items():
            # HTTP header names have case-insensitive identity.
            for mapping in (declared, private):
                for old_name in list(mapping):
                    if old_name == name or (field == "headers" and old_name.lower() == name.lower()):
                        mapping.pop(old_name)
            destination = declared if _TOKEN.search(value) else private
            destination[name] = value
        if declared:
            written[field] = declared
        else:
            written.pop(field, None)
        if private or field in cache:
            cache[field] = private


def write_server(key: ScopedName, entry: Optional[dict[str, Any]], *, replace: bool = True) -> None:
    """Validate the prospective document and private maps before publishing either.

    Supplied template values belong in the project file; concrete values belong
    in the private cache. Each supplied key replaces its previous destination.
    """

    with _DOCUMENT_LOCK:
        text, data = _document(key.scope)
        before = copy.deepcopy(data)
        servers = data.setdefault("mcpServers", {})
        cache = None
        previous_cache = None
        if entry is None:
            servers.pop(key.name, None)
        else:
            project_add_definition(key.name, entry)
            _apply_entry(servers, key, entry, replace)
            written = servers[key.name]
            previous_cache = _secrets(key)
            cache = copy.deepcopy(previous_cache)
            _split_values(written, cache, entry)
            cache = _check_secrets(cache)
            _effective(key, written, cache)
        if text is None or data != before:
            written = None if entry is None else servers.get(key.name)
            next_text = _edit_document_text(text, key, written, data)
            if next_text is None:
                next_text = _render_document(data)
            _write_document(_project_file(key.scope), next_text, text)
        if cache is not None and cache != previous_cache:
            _write_cache(key, "secrets.json", cache)


def remove_server(key: ScopedName) -> None:
    write_server(key, None)
    directory = repository.package_dir(key)
    if directory.exists():
        shutil.rmtree(directory)
    project_dir = key.scope.root
    if project_dir != directory and project_dir.exists() and next(project_dir.iterdir(), None) is None:
        project_dir.rmdir()
