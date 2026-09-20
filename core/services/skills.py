"""Business API for Agent Skills — a thin shell over the ``askill`` CLI.

Wraps ``askill <cmd> --json`` (github.com/avibe-bot/askill, v0.1.13+) so the
Web UI can manage one global or project Skill installation shared by every
backend without owning install logic. The CLI is the source of truth; this
layer maps Avibe concepts onto askill's flags, runs the binary, and parses the
documented ``--json`` contract into plain dicts.

Layering (per ``docs/plans/workbench-dispatch-architecture.md`` §6, and the
build plan in ``docs/plans/workbench-skills-page.md``):

* Transport-agnostic and dependency-injected: the resolved ``askill`` binary
  path is passed in by the caller (``vibe.api`` resolves it via
  ``resolve_cli_path("askill")``), so ``core/`` never imports ``vibe/``.
* Functions return plain ``dict`` payloads (the askill envelope). Failures
  raise ``LookupError("askill_not_found")`` or ``SkillsError(code, message)``
  for the route layer to translate.

Scope-flag note: ``list`` distinguishes ``-g`` / ``-p`` / (all); but
``add`` / ``remove`` / ``check`` / ``update`` only take ``-g`` for global —
project scope is the default and is selected by running with ``cwd`` set to
the project folder.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0

# avibe backend id <-> askill agent id. One map, used everywhere.
BACKEND_TO_AGENT: dict[str, str] = {
    "claude": "claude-code",
    "opencode": "opencode",
    "codex": "codex",
}
AGENT_TO_BACKEND: dict[str, str] = {agent: backend for backend, agent in BACKEND_TO_AGENT.items()}
_SKILL_RESOURCE_SCOPES = frozenset({"global", "project"})


class SkillsError(Exception):
    """A failure with a stable ``code`` the route layer maps to HTTP/i18n."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class SkillAccessError(SkillsError):
    """Raised when a remote caller cannot use or manage a Skill resource."""

    def __init__(self) -> None:
        super().__init__("resource_access_forbidden", "Skill access is not permitted.")


def _subprocess_env(askill_path: str) -> dict[str, str]:
    """Env for the askill subprocess with the binary's own dir leading PATH.

    askill is a Node CLI (``#!/usr/bin/env node``); when ``resolve_cli_path``
    finds an npm/nvm install outside the service PATH, the shebang still needs
    ``node`` — which lives alongside the askill binary — to be resolvable, else
    every Skills action fails with no output. Mirrors
    ``vibe.api._command_env_for`` (kept local so ``core`` stays free of ``vibe``
    imports).
    """
    env = {**os.environ, "PATH": os.environ.get("PATH", "")}
    binary_dir = os.path.dirname(os.path.abspath(askill_path))
    if binary_dir:
        entries = [e for e in env["PATH"].split(os.pathsep) if e and e != binary_dir]
        env["PATH"] = os.pathsep.join([binary_dir, *entries])
    return env


async def _run_askill(
    askill_path: str,
    args: list[str],
    *,
    cwd: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run ``askill <args> --json`` and parse stdout as JSON.

    In ``--json`` mode askill emits a machine-readable envelope even on a
    non-zero exit, so we parse stdout regardless of the return code and let
    callers branch on ``data["ok"]`` / ``data["error"]``. Spawn, timeout, and
    parse failures raise (``LookupError`` for a missing binary, ``SkillsError``
    otherwise).
    """
    if not askill_path:
        raise LookupError("askill_not_found")
    if cwd is not None and not os.path.isdir(cwd):
        # A deleted/moved project folder also makes create_subprocess_exec raise
        # FileNotFoundError; distinguish it from a missing askill binary so the
        # UI reports the actionable problem (the project path) not "not installed".
        raise SkillsError("project_dir_missing", f"project folder not found: {cwd}")
    cmd = [askill_path, *args, "--json"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=_subprocess_env(askill_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as err:
        raise LookupError("askill_not_found") from err
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        await proc.communicate()
        logger.info("askill timed out after %ss: %s", timeout, " ".join(args))
        raise SkillsError("askill_timeout", f"askill timed out after {timeout:.0f}s")

    text = (out or b"").decode("utf-8", errors="replace").strip()
    if not text:
        detail = (err or b"").decode("utf-8", errors="replace").strip()
        logger.info("askill produced no stdout (%s): %s", " ".join(args), detail[:300])
        raise SkillsError("askill_no_output", detail or "askill produced no output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.info("askill emitted non-JSON output (%s): %s", " ".join(args), text[:300])
        raise SkillsError("askill_bad_json", "could not parse askill output") from exc
    if not isinstance(data, dict):
        raise SkillsError("askill_bad_json", "askill output was not a JSON object")
    return data


def _agent_flags(backends: Optional[list[str]]) -> list[str]:
    """Expand selected Vibe backends into a single variadic ``-a`` flag.

    askill parses ``-a, --agent <agents...>`` as one variadic option and each
    later ``-a`` *replaces* the previous values (``options.agent = values``), so
    multiple agents must share one flag — ``-a claude-code opencode`` — not
    repeated ``-a`` flags, or only the last agent would receive the operation.
    """
    agents: list[str] = []
    for backend in backends or []:
        agent = BACKEND_TO_AGENT.get(backend)
        if not agent:
            raise SkillsError("invalid_backend", f"unknown backend: {backend}")
        agents.append(agent)
    return ["-a", *agents] if agents else []


def _list_scope_flag(scope: str) -> list[str]:
    """Scope flags for ``list`` (supports -g / -p / all)."""
    if scope == "global":
        return ["-g"]
    if scope == "project":
        return ["-p"]
    if scope == "all":
        return []
    raise SkillsError("invalid_scope", f"unknown scope: {scope}")


def _target_scope_flag(scope: str) -> list[str]:
    """Scope flag for ``add`` / ``remove`` / ``check`` / ``update``.

    These commands only take ``-g`` for global; project scope is the default
    and is selected by running with ``cwd`` = the project folder (no flag).
    """
    if scope == "global":
        return ["-g"]
    if scope == "project":
        return []
    raise SkillsError("invalid_scope", f"unknown scope: {scope}")


def _cwd_for(scope: str, project_dir: Optional[str]) -> Optional[str]:
    # Project scope is selected by running in the project folder; refuse to fall
    # back to the server's own cwd when a project-scoped op arrives without one.
    if scope == "project" and not project_dir:
        raise SkillsError("project_required", "a project is required for project-scoped skills")
    return project_dir if scope != "global" else None


def resolve_resource_access_context(user_context: Any = None):
    """Resolve request context for Skill ACL checks."""

    from storage import resource_access_service

    return resource_access_service.resolve_resource_access_context(user_context)


def _normalize_skill_name(name: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    if not normalized:
        raise SkillsError("invalid_skill", "skill name is required")
    return normalized


def _backend_from_agent_ref(value: Any) -> str | None:
    candidates: list[Any]
    if isinstance(value, dict):
        candidates = [value.get("backend"), value.get("id"), value.get("name")]
    else:
        candidates = [value]
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower()
        if normalized in BACKEND_TO_AGENT:
            return normalized
        if normalized in AGENT_TO_BACKEND:
            return AGENT_TO_BACKEND[normalized]
    return None


def _normalized_backends(backends: Optional[list[str]]) -> list[str]:
    result: list[str] = []
    for backend in backends or []:
        normalized = _backend_from_agent_ref(backend)
        if normalized is None:
            raise SkillsError("invalid_backend", f"unknown backend: {backend}")
        if normalized not in result:
            result.append(normalized)
    return result


def _normalize_project_id(project_id: Any) -> str:
    cleaned = str(project_id or "").strip()
    if (
        not cleaned
        or len(cleaned) > 64
        or any(ord(char) < 32 or ord(char) == 127 for char in cleaned)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", cleaned)
    ):
        raise SkillsError("invalid_project_id", "project id is required")
    return cleaned


def _project_resource_segment(
    scope: str,
    project_dir: Optional[str],
    *,
    project_id: Optional[str] = None,
) -> str:
    if scope == "global":
        return "global"
    if scope != "project":
        raise SkillsError("invalid_scope", f"unknown scope: {scope}")
    if project_id:
        return f"project-{_normalize_project_id(project_id)}"
    if not project_dir:
        raise SkillsError("project_required", "a project is required for project-scoped skills")
    canonical_path = os.path.normcase(os.path.realpath(os.path.abspath(project_dir)))
    return f"project-{hashlib.sha256(canonical_path.encode('utf-8')).hexdigest()[:24]}"


def skill_resource_id(
    backend: str,
    *,
    scope: str,
    project_dir: Optional[str],
    project_id: Optional[str] = None,
    name: str,
) -> str:
    """Return the stable local ACL descriptor for one backend-specific Skill."""

    normalized_backend = _backend_from_agent_ref(backend)
    if normalized_backend is None:
        raise SkillsError("invalid_backend", f"unknown backend: {backend}")
    if scope not in _SKILL_RESOURCE_SCOPES:
        raise SkillsError("invalid_scope", f"unknown scope: {scope}")
    return ":".join(
        [
            normalized_backend,
            scope,
            _project_resource_segment(scope, project_dir, project_id=project_id),
            _normalize_skill_name(name),
        ]
    )


def get_skill_resource_metadata(resource_id: str) -> dict[str, str] | None:
    """Resolve the display name encoded by a validated Skill resource ID."""

    parts = str(resource_id or "").split(":")
    if len(parts) != 4:
        return None
    backend, scope, project_segment, name = parts
    if (
        _backend_from_agent_ref(backend) != backend
        or scope not in _SKILL_RESOURCE_SCOPES
        or not project_segment
    ):
        return None
    try:
        if _normalize_skill_name(name) != name:
            return None
    except SkillsError:
        return None
    return {"display_name": name}


def _skill_scope(skill: dict[str, Any], requested_scope: str) -> str | None:
    scope = str(skill.get("scope") or "").strip().lower()
    if scope in _SKILL_RESOURCE_SCOPES:
        return scope
    if requested_scope in _SKILL_RESOURCE_SCOPES:
        return requested_scope
    return None


def _skill_backend_entries(skill: dict[str, Any], selected_backends: Optional[list[str]]) -> list[tuple[str, int | None]]:
    raw_agents = skill.get("agents")
    if isinstance(raw_agents, list):
        result: list[tuple[str, int | None]] = []
        for index, agent in enumerate(raw_agents):
            backend = _backend_from_agent_ref(agent)
            if backend is not None:
                result.append((backend, index))
        if selected_backends:
            selected = set(_normalized_backends(selected_backends))
            result = [item for item in result if item[0] in selected]
        return result
    return [(backend, None) for backend in _normalized_backends(selected_backends)]


def _skill_resource_descriptors(
    skills: list[dict[str, Any]],
    *,
    requested_scope: str,
    project_dir: Optional[str],
    project_id: Optional[str],
    backends: Optional[list[str]],
) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for skill_index, skill in enumerate(skills):
        scope = _skill_scope(skill, requested_scope)
        name = skill.get("name")
        if scope is None or not isinstance(name, str):
            continue
        try:
            entries = _skill_backend_entries(skill, backends)
            for backend, agent_index in entries:
                descriptors.append(
                    {
                        "id": skill_resource_id(
                            backend,
                            scope=scope,
                            project_dir=project_dir,
                            project_id=project_id,
                            name=name,
                        ),
                        "skill_index": skill_index,
                        "agent_index": agent_index,
                    }
                )
        except SkillsError:
            # A malformed askill row must not become visible to a remote caller.
            continue
    return descriptors


def _filter_skill_listing(
    result: dict[str, Any],
    *,
    scope: str,
    project_dir: Optional[str],
    project_id: Optional[str],
    backends: Optional[list[str]],
    user_context: Any = None,
) -> dict[str, Any]:
    """Filter askill's value-free list payload through local Skill ACLs."""

    if not result.get("ok") or not isinstance(result.get("skills"), list):
        return result
    context = resolve_resource_access_context(user_context)
    project_editor = _project_role_allows_editor(context, project_id)
    instance_editor = context.has_role("editor")
    if (project_editor and instance_editor) or (project_editor and scope == "project") or (
        instance_editor and scope == "global"
    ):
        return result

    raw_skills = [dict(skill) for skill in result["skills"] if isinstance(skill, dict)]
    descriptors = _skill_resource_descriptors(
        raw_skills,
        requested_scope=scope,
        project_dir=project_dir,
        project_id=project_id,
        backends=backends,
    )
    allowed: set[tuple[int, int | None]] = set()
    if descriptors:
        from storage import resource_access_service
        from storage.db import get_cached_sqlite_engine

        engine = get_cached_sqlite_engine()
        with engine.connect() as connection:
            accessible = resource_access_service.filter_accessible_resources(
                context,
                "skill",
                descriptors,
                connection=connection,
            )
        allowed = {(item["skill_index"], item["agent_index"]) for item in accessible}

    filtered_skills: list[dict[str, Any]] = []
    for skill_index, skill in enumerate(raw_skills):
        skill_scope = _skill_scope(skill, scope)
        if (project_editor and skill_scope == "project") or (
            instance_editor and skill_scope == "global"
        ):
            filtered_skills.append(skill)
            continue
        matching = [item for item in descriptors if item["skill_index"] == skill_index]
        if not matching or not any(
            (item["skill_index"], item["agent_index"]) in allowed for item in matching
        ):
            continue
        raw_agents = skill.get("agents")
        if isinstance(raw_agents, list):
            skill["agents"] = [
                agent
                for agent_index, agent in enumerate(raw_agents)
                if (skill_index, agent_index) in allowed
            ]
            if not skill["agents"]:
                continue
        filtered_skills.append(_remote_safe_skill_payload(skill))

    filtered = dict(result)
    filtered["skills"] = filtered_skills
    if isinstance(result.get("summary"), dict):
        summary = dict(result["summary"])
        summary["global"] = sum(1 for skill in filtered_skills if skill.get("scope") == "global")
        summary["project"] = sum(1 for skill in filtered_skills if skill.get("scope") == "project")
        filtered["summary"] = summary
    return filtered


def _remote_safe_skill_payload(skill: dict[str, Any]) -> dict[str, Any]:
    """Return the installed-Skill fields safe to expose across the tunnel."""

    projected = {
        key: skill[key]
        for key in (
            "name",
            "scope",
            "description",
            "version",
            "tags",
            "sourceType",
            "installedAt",
            "updatedAt",
            "status",
            "localVersion",
            "remoteVersion",
        )
        if key in skill
    }
    projected["path"] = ""
    raw_agents = skill.get("agents")
    if isinstance(raw_agents, list):
        projected["agents"] = [
            {key: agent[key] for key in ("id", "name") if key in agent}
            for agent in raw_agents
            if isinstance(agent, dict)
        ]
    return projected


def _project_role_allows_editor(user_context: Any, project_id: Optional[str]) -> bool:
    """Return whether the caller has effective Editor access to a project."""

    context = resolve_resource_access_context(user_context)
    if context.is_instance_owner:
        return True
    if not project_id:
        return context.has_role("editor")

    from storage import project_access_service
    from storage.db import get_cached_sqlite_engine

    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        role = project_access_service.get_effective_project_role(
            connection,
            context,
            str(project_id),
        )
    return project_access_service.role_allows(role, "editor")


def require_project_editor_access(user_context: Any, project_id: Optional[str]) -> None:
    """Require effective project Editor access for a project-scoped mutation."""

    if project_id and not _project_role_allows_editor(user_context, project_id):
        raise SkillAccessError()


def _resource_ids_for_skill_name(
    name: str,
    *,
    scope: str,
    project_dir: Optional[str],
    project_id: Optional[str],
    backends: Optional[list[str]],
) -> list[str]:
    return [
        skill_resource_id(
            backend,
            scope=scope,
            project_dir=project_dir,
            project_id=project_id,
            name=name,
        )
        for backend in _normalized_backends(backends) or list(BACKEND_TO_AGENT)
    ]


def _require_skill_management_access(
    resource_ids: list[str],
    *,
    user_context: Any,
    allow_missing_policy: bool,
) -> None:
    from storage import resource_access_service
    from storage.db import get_cached_sqlite_engine

    context = resolve_resource_access_context(user_context)
    if context.has_role("editor"):
        return
    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        found_policy = False
        for resource_id in resource_ids:
            policy = resource_access_service.get_resource_policy("skill", resource_id, connection=connection)
            if policy is None:
                continue
            found_policy = True
            if resource_access_service.can_manage_resource_acl(
                context,
                "skill",
                resource_id,
                connection=connection,
            ):
                # Backend ids are legacy aliases for one logical Skill. Any
                # manageable alias grants the same unified operation.
                return
        if not found_policy and (allow_missing_policy or context.is_instance_owner):
            return
        raise SkillAccessError()


def _require_skill_create_access(user_context: Any) -> None:
    context = resolve_resource_access_context(user_context)
    if context.has_role("editor"):
        return
    raise SkillAccessError()


def _require_skill_use_access(
    context: Any,
    *,
    scope: str,
    project_dir: Optional[str],
) -> None:
    """Require Instance use and Project chat access before invoking askill."""

    if (
        not context.can_use_resource("skill")
        and not context.has_role("editor")
    ):
        raise SkillAccessError()
    if context.has_role("editor"):
        return
    if not project_dir:
        if scope in {"all", "global"}:
            return
        raise SkillAccessError()

    from sqlalchemy import select

    from storage import project_access_service
    from storage.db import get_cached_sqlite_engine
    from storage.models import scope_settings, scopes

    resolved_dir = os.path.realpath(os.path.abspath(os.path.expanduser(project_dir)))
    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        project_id = connection.execute(
            select(scopes.c.native_id)
            .select_from(scopes.join(scope_settings, scope_settings.c.scope_id == scopes.c.id))
            .where(
                scopes.c.platform == "avibe",
                scopes.c.scope_type == "project",
                scope_settings.c.workdir == resolved_dir,
            )
            .limit(1)
        ).scalar_one_or_none()
        role = (
            project_access_service.get_effective_project_role(
                connection,
                context,
                str(project_id),
            )
            if project_id
            else None
        )
    if not project_access_service.role_allows(role, "editor"):
        raise SkillAccessError()


def _skill_names_from_payload(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    direct = payload.get("skill")
    if isinstance(direct, str):
        names.append(direct)
    for key in ("skills", "results"):
        entries = payload.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                names.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            for candidate in (entry.get("skill"), entry.get("name")):
                if isinstance(candidate, str):
                    names.append(candidate)
                    break
    return list(dict.fromkeys(name for name in names if name.strip()))


def _register_created_skill_policies(
    names: list[str],
    *,
    scope: str,
    project_dir: Optional[str],
    project_id: Optional[str],
    backends: list[str],
    user_context: Any,
) -> None:
    from storage import resource_access_service
    from storage.db import get_cached_sqlite_engine

    context = resolve_resource_access_context(user_context)
    if not context.subject:
        return
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        for name in names:
            for backend in backends:
                resource_access_service.ensure_resource_policy(
                    connection,
                    resource_kind="skill",
                    resource_id=skill_resource_id(
                        backend,
                        scope=scope,
                        project_dir=project_dir,
                        project_id=project_id,
                        name=name,
                    ),
                    organization_id=context.organization_id,
                    owner_user_id=context.subject,
                    owner_email=context.email,
                    access_level="private",
                    created_by_user_id=context.subject,
                    updated_by_user_id=context.subject,
                )


def _delete_skill_policies(resource_ids: list[str]) -> None:
    if not resource_ids:
        return
    from storage import resource_access_service
    from storage.db import get_cached_sqlite_engine
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        for resource_id in resource_ids:
            resource_access_service.delete_resource_policy(connection, "skill", resource_id)


def _installed_skill_resource_ids_from_listing(
    listing: dict[str, Any],
    name: str,
    *,
    scope: str,
    project_dir: Optional[str],
    project_id: Optional[str],
    backends: Optional[list[str]],
) -> list[str]:
    if not listing.get("ok") or not isinstance(listing.get("skills"), list):
        raise SkillAccessError()
    target_name = _normalize_skill_name(name)
    resource_ids: list[str] = []
    for skill in listing["skills"]:
        if not isinstance(skill, dict):
            continue
        try:
            row_name = _normalize_skill_name(skill.get("name"))
        except SkillsError:
            continue
        if row_name != target_name:
            continue
        row_scope = _skill_scope(skill, scope)
        if row_scope is None:
            continue
        all_entries = _skill_backend_entries(skill, None)
        if not all_entries:
            raise SkillAccessError()
        entries = _skill_backend_entries(skill, backends) if backends else all_entries
        for backend, _agent_index in entries:
            resource_ids.append(
                skill_resource_id(
                    backend,
                    scope=row_scope,
                    project_dir=project_dir,
                    project_id=project_id,
                    name=str(skill["name"]),
                )
            )
    return list(dict.fromkeys(resource_ids))


async def _installed_skill_resource_ids(
    askill_path: str,
    name: str,
    *,
    scope: str,
    project_dir: Optional[str],
    project_id: Optional[str],
    backends: Optional[list[str]],
) -> list[str]:
    listing = await _run_askill(
        askill_path,
        ["list", *_list_scope_flag(scope), *_agent_flags(backends)],
        cwd=_cwd_for(scope, project_dir),
    )
    resource_ids = _installed_skill_resource_ids_from_listing(
        listing,
        name,
        scope=scope,
        project_dir=project_dir,
        project_id=project_id,
        backends=backends,
    )
    if not resource_ids:
        raise SkillAccessError()
    return resource_ids


# --- public API -----------------------------------------------------------


async def list_skills(
    askill_path: str,
    *,
    scope: str = "all",
    project_dir: Optional[str] = None,
    project_id: Optional[str] = None,
    backends: Optional[list[str]] = None,
    user_context: Any = None,
) -> dict[str, Any]:
    """List installed skills. ``scope`` is ``all`` | ``global`` | ``project``.

    Project-scoped lists run with ``cwd=project_dir`` so askill resolves the
    repo's ``.agents/skills``. Each item carries description / version / tags /
    source / installSource / timestamps natively (askill v0.1.13+).
    """
    context = resolve_resource_access_context(user_context)
    _require_skill_use_access(context, scope=scope, project_dir=project_dir)
    _agent_flags(backends)  # Retained request field: validate, then apply unified semantics.
    args = ["list", *_list_scope_flag(scope)]
    result = await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))
    return _filter_skill_listing(
        result,
        scope=scope,
        project_dir=project_dir,
        project_id=project_id,
        backends=None,
        user_context=context,
    )


async def preview_source(
    askill_path: str,
    source: str,
    *,
    project_dir: Optional[str] = None,
    project_id: Optional[str] = None,
    user_context: Any = None,
) -> dict[str, Any]:
    """Discover the skills a source contains without installing.

    Maps to ``askill add <source> --list --json``. ``source`` is a slug
    (``gh:owner/repo[@name]``), a GitHub URL, or a local directory.
    """
    if not source:
        raise SkillsError("missing_source", "no source provided")
    context = resolve_resource_access_context(user_context)
    if project_dir and project_id:
        require_project_editor_access(context, project_id)
    else:
        _require_skill_create_access(context)
    return await _run_askill(askill_path, ["add", source, "--list"], cwd=project_dir)


async def add_skill(
    askill_path: str,
    source: str,
    *,
    scope: str = "project",
    project_dir: Optional[str] = None,
    project_id: Optional[str] = None,
    backends: Optional[list[str]] = None,
    all_skills: bool = False,
    skill: Optional[str] = None,
    copy: bool = False,
    user_context: Any = None,
) -> dict[str, Any]:
    """Install skill(s) from a source. Non-interactive (``-y``).

    ``askill add <source> [-g] -a claude-code opencode codex [--all|--skill <name>] [--copy] -y``.
    ``skill`` installs one named skill from a multi-skill source (use this for
    local dirs, where ``source@name`` is ambiguous); ``all_skills`` installs
    every discovered skill. ``scope`` must be ``global`` or ``project``.
    """
    if not source:
        raise SkillsError("missing_source", "no source provided")
    if scope not in ("global", "project"):
        raise SkillsError("invalid_scope", "install scope must be global or project")
    _agent_flags(backends)  # Backward-compatible input validation; selection is intentionally ignored.
    context = resolve_resource_access_context(user_context)
    if scope == "project":
        require_project_editor_access(context, project_id)
    if not (
        context.is_instance_owner
        or context.has_role("editor")
    ):
        _require_skill_create_access(context)
        target_names = [skill] if skill else []
        if not target_names:
            preview = await _run_askill(askill_path, ["add", source, "--list"], cwd=_cwd_for(scope, project_dir))
            if preview.get("ok"):
                target_names = _skill_names_from_payload(preview)
        if not target_names:
            raise SkillAccessError()
        for target_name in target_names:
            _require_skill_management_access(
                _resource_ids_for_skill_name(
                    target_name,
                    scope=scope,
                    project_dir=project_dir,
                    project_id=project_id,
                    backends=None,
                ),
                user_context=context,
                allow_missing_policy=True,
            )
        listing = await _run_askill(
            askill_path,
            ["list", *_list_scope_flag(scope)],
            cwd=_cwd_for(scope, project_dir),
        )
        for target_name in target_names:
            installed_resource_ids = _installed_skill_resource_ids_from_listing(
                listing,
                target_name,
                scope=scope,
                project_dir=project_dir,
                project_id=project_id,
                backends=None,
            )
            if installed_resource_ids:
                _require_skill_management_access(
                    installed_resource_ids,
                    user_context=context,
                    allow_missing_policy=False,
                )
    args = [
        "add",
        source,
        *_target_scope_flag(scope),
        *_agent_flags(list(BACKEND_TO_AGENT)),
    ]
    if skill:
        args += ["--skill", skill]
    if all_skills:
        args.append("--all")
    if copy:
        args.append("--copy")
    args.append("-y")
    result = await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))
    # askill returns ok=True even when a `@name` selector (or empty source)
    # matched no skill — it just installs nothing (results/summary null). Surface
    # that as a failure so the UI never reports success for a skill that never
    # landed (e.g. ``gh:owner/repo@does-not-exist``).
    if result.get("ok") and result.get("action") == "install":
        summary = result.get("summary")
        installed = (
            summary.get("skills")
            if isinstance(summary, dict) and isinstance(summary.get("skills"), int)
            else sum(1 for r in (result.get("results") or []) if isinstance(r, dict) and r.get("success"))
        )
        if not installed:
            return {
                "ok": False,
                "error": {
                    "code": "nothing_installed",
                    "message": "No matching skill was found in this source — nothing was installed.",
                },
            }
    if result.get("ok"):
        names = _skill_names_from_payload(result)
        if not names and skill:
            names = [skill]
        if names:
            _register_created_skill_policies(
                names,
                scope=scope,
                project_dir=project_dir,
                project_id=project_id,
                backends=list(BACKEND_TO_AGENT),
                user_context=context,
            )
    return result


async def remove_skill(
    askill_path: str,
    name: str,
    *,
    scope: str = "project",
    project_dir: Optional[str] = None,
    project_id: Optional[str] = None,
    backends: Optional[list[str]] = None,
    user_context: Any = None,
) -> dict[str, Any]:
    """Remove one logical Skill installation from every managed backend link.

    The optional legacy ``backends`` field is validated but no longer narrows removal.
    """
    if not name:
        raise SkillsError("missing_skill", "no skill name provided")
    if scope not in ("global", "project"):
        raise SkillsError("invalid_scope", "remove scope must be global or project")
    _agent_flags(backends)
    context = resolve_resource_access_context(user_context)
    if scope == "project":
        require_project_editor_access(context, project_id)
    if not (
        context.is_instance_owner
        or context.has_role("editor")
    ):
        _require_skill_create_access(context)
        resource_ids = await _installed_skill_resource_ids(
            askill_path,
            name,
            scope=scope,
            project_dir=project_dir,
            project_id=project_id,
            backends=None,
        )
        _require_skill_management_access(
            resource_ids,
            user_context=context,
            allow_missing_policy=False,
        )
    args = [
        "remove",
        name,
        *_target_scope_flag(scope),
        *_agent_flags(list(BACKEND_TO_AGENT)),
    ]
    result = await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))
    if result.get("ok"):
        _delete_skill_policies(
            _resource_ids_for_skill_name(
                name,
                scope=scope,
                project_dir=project_dir,
                project_id=project_id,
                backends=None,
            )
        )
    return result


async def find_skills(
    askill_path: str,
    query: str = "",
    *,
    user_context: Any = None,
) -> dict[str, Any]:
    """Search the askill.sh registry. Maps to ``askill find <query>``.

    Returns ``{ok, query, filters, sort, pagination, count, skills[]}`` where
    each skill carries ``aiScore`` / ``aiBreakdown`` / ``stars`` / ``tags``.
    """
    _require_skill_create_access(resolve_resource_access_context(user_context))
    args = ["find"]
    if query:
        args.append(query)
    return await _run_askill(askill_path, args)


async def check(
    askill_path: str,
    *,
    scope: str = "project",
    project_dir: Optional[str] = None,
    project_id: Optional[str] = None,
    user_context: Any = None,
) -> dict[str, Any]:
    """Check installed skills for available updates (no install).

    Maps to ``askill check [-g] --json``. Returns ``{ok, summary, skills[]}``;
    each skill has ``status`` (``update_available`` | ``up_to_date`` |
    ``uncheckable``) plus ``localVersion`` / ``remoteVersion``.
    """
    context = resolve_resource_access_context(user_context)
    _require_skill_use_access(context, scope=scope, project_dir=project_dir)
    args = ["check", *_target_scope_flag(scope)]
    result = await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))
    filtered = _filter_skill_listing(
        result,
        scope=scope,
        project_dir=project_dir,
        project_id=project_id,
        backends=list(BACKEND_TO_AGENT),
        user_context=context,
    )
    if _project_role_allows_editor(context, project_id) or not isinstance(filtered.get("summary"), dict):
        return filtered
    skills = filtered.get("skills") if isinstance(filtered.get("skills"), list) else []
    statuses = [str(skill.get("status") or "") for skill in skills if isinstance(skill, dict)]
    filtered["summary"] = {
        "total": len(skills),
        "updateAvailable": statuses.count("update_available"),
        "upToDate": statuses.count("up_to_date"),
        "uncheckable": statuses.count("uncheckable"),
    }
    return filtered


async def update(
    askill_path: str,
    name: str,
    *,
    scope: str = "project",
    project_dir: Optional[str] = None,
    project_id: Optional[str] = None,
    user_context: Any = None,
) -> dict[str, Any]:
    """Update one installed skill. Maps to ``askill update <name> [-g] -y``."""
    if not name:
        raise SkillsError("missing_skill", "no skill name provided")
    if scope not in ("global", "project"):
        raise SkillsError("invalid_scope", "update scope must be global or project")
    context = resolve_resource_access_context(user_context)
    if scope == "project":
        require_project_editor_access(context, project_id)
    if not (
        context.is_instance_owner
        or context.has_role("editor")
    ):
        _require_skill_create_access(context)
        resource_ids = await _installed_skill_resource_ids(
            askill_path,
            name,
            scope=scope,
            project_dir=project_dir,
            project_id=project_id,
            backends=None,
        )
        _require_skill_management_access(
            resource_ids,
            user_context=context,
            allow_missing_policy=False,
        )
    args = ["update", name, *_target_scope_flag(scope), "-y"]
    return await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))
