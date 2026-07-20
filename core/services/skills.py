"""Business API for Agent Skills — a thin shell over the ``askill`` CLI.

Wraps ``askill <cmd> --json`` (github.com/avibe-bot/askill, v0.1.13+) so the
Web UI can manage global + project skills across backends without owning
install logic. The CLI is the source of truth; this layer maps avibe
concepts onto askill's flags, runs the binary, and parses the documented
``--json`` contract into plain dicts.

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
from typing import Any, Optional

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
    """Resolve request ACL context while preserving trusted local Skill use."""

    from storage import resource_access_service

    if isinstance(user_context, resource_access_service.ResourceUserContext):
        return user_context
    if user_context is not None:
        return resource_access_service.current_resource_context(user_context, is_remote=True)

    context = resource_access_service.current_resource_context()
    if context.is_remote or context.is_trusted_local:
        return context
    return resource_access_service.ResourceUserContext(is_trusted_local=True)


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


def _project_resource_segment(scope: str, project_dir: Optional[str]) -> str:
    if scope == "global":
        return "global"
    if scope != "project":
        raise SkillsError("invalid_scope", f"unknown scope: {scope}")
    if not project_dir:
        raise SkillsError("project_required", "a project is required for project-scoped skills")
    canonical_path = os.path.normcase(os.path.realpath(os.path.abspath(project_dir)))
    return f"project-{hashlib.sha256(canonical_path.encode('utf-8')).hexdigest()[:24]}"


def skill_resource_id(
    backend: str,
    *,
    scope: str,
    project_dir: Optional[str],
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
            _project_resource_segment(scope, project_dir),
            _normalize_skill_name(name),
        ]
    )


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
    backends: Optional[list[str]],
    user_context: Any = None,
) -> dict[str, Any]:
    """Filter askill's value-free list payload through local Skill ACLs."""

    if not result.get("ok") or not isinstance(result.get("skills"), list):
        return result
    context = resolve_resource_access_context(user_context)
    if context.is_trusted_local:
        return result

    raw_skills = [dict(skill) for skill in result["skills"] if isinstance(skill, dict)]
    descriptors = _skill_resource_descriptors(
        raw_skills,
        requested_scope=scope,
        project_dir=project_dir,
        backends=backends,
    )
    if not descriptors:
        filtered_skills: list[dict[str, Any]] = []
    else:
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
        filtered_skills = []
        for skill_index, skill in enumerate(raw_skills):
            matching = [item for item in descriptors if item["skill_index"] == skill_index]
            if not matching or not any((item["skill_index"], item["agent_index"]) in allowed for item in matching):
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
            filtered_skills.append(skill)

    filtered = dict(result)
    filtered["skills"] = filtered_skills
    if isinstance(result.get("summary"), dict):
        summary = dict(result["summary"])
        summary["global"] = sum(1 for skill in filtered_skills if skill.get("scope") == "global")
        summary["project"] = sum(1 for skill in filtered_skills if skill.get("scope") == "project")
        filtered["summary"] = summary
    return filtered


def _resource_ids_for_skill_name(
    name: str,
    *,
    scope: str,
    project_dir: Optional[str],
    backends: Optional[list[str]],
) -> list[str]:
    return [
        skill_resource_id(backend, scope=scope, project_dir=project_dir, name=name)
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
    if context.is_trusted_local:
        return
    engine = get_cached_sqlite_engine()
    with engine.connect() as connection:
        for resource_id in resource_ids:
            policy = resource_access_service.get_resource_policy("skill", resource_id, connection=connection)
            if policy is None:
                if allow_missing_policy or context.is_instance_owner:
                    continue
                raise SkillAccessError()
            if not resource_access_service.can_manage_resource_acl(
                context,
                "skill",
                resource_id,
                connection=connection,
            ):
                raise SkillAccessError()


def _require_skill_create_access(user_context: Any) -> None:
    context = resolve_resource_access_context(user_context)
    if context.is_trusted_local or context.is_instance_owner:
        return
    if context.is_remote and context.is_active_organization_member and context.subject:
        return
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


def _result_backends(result: dict[str, Any], fallback: Optional[list[str]]) -> list[str]:
    for key in ("selectedAgents", "agents", "removedAgents"):
        raw_agents = result.get(key)
        if not isinstance(raw_agents, list):
            continue
        resolved = [backend for agent in raw_agents if (backend := _backend_from_agent_ref(agent)) is not None]
        if resolved:
            return list(dict.fromkeys(resolved))
    return _normalized_backends(fallback) or list(BACKEND_TO_AGENT)


def _register_created_skill_policies(
    names: list[str],
    *,
    scope: str,
    project_dir: Optional[str],
    backends: list[str],
    user_context: Any,
) -> None:
    from storage import resource_access_service
    from storage.db import get_cached_sqlite_engine

    context = resolve_resource_access_context(user_context)
    if not (context.is_remote and context.is_active_organization_member and context.subject):
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
                        name=name,
                    ),
                    organization_id=context.organization_id,
                    owner_user_id=context.subject,
                    owner_email=context.email,
                    access_level="private",
                    created_by_user_id=context.subject,
                    updated_by_user_id=context.subject,
                )


async def _installed_skill_resource_ids(
    askill_path: str,
    name: str,
    *,
    scope: str,
    project_dir: Optional[str],
    backends: Optional[list[str]],
) -> list[str]:
    listing = await _run_askill(
        askill_path,
        ["list", *_list_scope_flag(scope), *_agent_flags(backends)],
        cwd=_cwd_for(scope, project_dir),
    )
    raw_skills = listing.get("skills") if isinstance(listing.get("skills"), list) else []
    target_name = _normalize_skill_name(name)
    resource_ids: list[str] = []
    for skill in raw_skills:
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
        entries = _skill_backend_entries(skill, backends)
        if not entries:
            raise SkillAccessError()
        for backend, _agent_index in entries:
            resource_ids.append(
                skill_resource_id(
                    backend,
                    scope=row_scope,
                    project_dir=project_dir,
                    name=str(skill["name"]),
                )
            )
    return list(dict.fromkeys(resource_ids))


# --- public API -----------------------------------------------------------


async def list_skills(
    askill_path: str,
    *,
    scope: str = "all",
    project_dir: Optional[str] = None,
    backends: Optional[list[str]] = None,
    user_context: Any = None,
) -> dict[str, Any]:
    """List installed skills. ``scope`` is ``all`` | ``global`` | ``project``.

    Project-scoped lists run with ``cwd=project_dir`` so askill resolves the
    repo's ``.agents/skills``. Each item carries description / version / tags /
    source / installSource / timestamps natively (askill v0.1.13+).
    """
    args = ["list", *_list_scope_flag(scope), *_agent_flags(backends)]
    result = await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))
    return _filter_skill_listing(
        result,
        scope=scope,
        project_dir=project_dir,
        backends=backends,
        user_context=user_context,
    )


async def preview_source(
    askill_path: str,
    source: str,
    *,
    project_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Discover the skills a source contains without installing.

    Maps to ``askill add <source> --list --json``. ``source`` is a slug
    (``gh:owner/repo[@name]``), a GitHub URL, or a local directory.
    """
    if not source:
        raise SkillsError("missing_source", "no source provided")
    return await _run_askill(askill_path, ["add", source, "--list"], cwd=project_dir)


async def add_skill(
    askill_path: str,
    source: str,
    *,
    scope: str = "project",
    project_dir: Optional[str] = None,
    backends: Optional[list[str]] = None,
    all_skills: bool = False,
    skill: Optional[str] = None,
    copy: bool = False,
    user_context: Any = None,
) -> dict[str, Any]:
    """Install skill(s) from a source. Non-interactive (``-y``).

    ``askill add <source> [-g] [-a <agent>...] [--all|--skill <name>] [--copy] -y``.
    ``skill`` installs one named skill from a multi-skill source (use this for
    local dirs, where ``source@name`` is ambiguous); ``all_skills`` installs
    every discovered skill. ``scope`` must be ``global`` or ``project``.
    """
    if not source:
        raise SkillsError("missing_source", "no source provided")
    if scope not in ("global", "project"):
        raise SkillsError("invalid_scope", "install scope must be global or project")
    context = resolve_resource_access_context(user_context)
    if not context.is_trusted_local:
        _require_skill_create_access(context)
        target_names = [skill] if skill else []
        if not target_names:
            preview = await _run_askill(askill_path, ["add", source, "--list"], cwd=_cwd_for(scope, project_dir))
            if preview.get("ok"):
                target_names = _skill_names_from_payload(preview)
        for target_name in target_names:
            _require_skill_management_access(
                _resource_ids_for_skill_name(
                    target_name,
                    scope=scope,
                    project_dir=project_dir,
                    backends=backends,
                ),
                user_context=context,
                allow_missing_policy=True,
            )
    args = ["add", source, *_target_scope_flag(scope), *_agent_flags(backends)]
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
                backends=_result_backends(result, backends),
                user_context=context,
            )
    return result


async def remove_skill(
    askill_path: str,
    name: str,
    *,
    scope: str = "project",
    project_dir: Optional[str] = None,
    backends: Optional[list[str]] = None,
    user_context: Any = None,
) -> dict[str, Any]:
    """Remove an installed skill, optionally from specific backends only.

    Maps to ``askill remove <name> [-g] [-a <agent>...]``.
    """
    if not name:
        raise SkillsError("missing_skill", "no skill name provided")
    if scope not in ("global", "project"):
        raise SkillsError("invalid_scope", "remove scope must be global or project")
    context = resolve_resource_access_context(user_context)
    if not context.is_trusted_local:
        resource_ids = await _installed_skill_resource_ids(
            askill_path,
            name,
            scope=scope,
            project_dir=project_dir,
            backends=backends,
        )
        if resource_ids:
            _require_skill_management_access(
                resource_ids,
                user_context=context,
                allow_missing_policy=False,
            )
    args = ["remove", name, *_target_scope_flag(scope), *_agent_flags(backends)]
    return await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))


async def find_skills(askill_path: str, query: str = "") -> dict[str, Any]:
    """Search the askill.sh registry. Maps to ``askill find <query>``.

    Returns ``{ok, query, filters, sort, pagination, count, skills[]}`` where
    each skill carries ``aiScore`` / ``aiBreakdown`` / ``stars`` / ``tags``.
    """
    args = ["find"]
    if query:
        args.append(query)
    return await _run_askill(askill_path, args)


async def check(
    askill_path: str,
    *,
    scope: str = "project",
    project_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Check installed skills for available updates (no install).

    Maps to ``askill check [-g] --json``. Returns ``{ok, summary, skills[]}``;
    each skill has ``status`` (``update_available`` | ``up_to_date`` |
    ``uncheckable``) plus ``localVersion`` / ``remoteVersion``.
    """
    args = ["check", *_target_scope_flag(scope)]
    return await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))


async def update(
    askill_path: str,
    name: str,
    *,
    scope: str = "project",
    project_dir: Optional[str] = None,
    user_context: Any = None,
) -> dict[str, Any]:
    """Update one installed skill. Maps to ``askill update <name> [-g] -y``."""
    if not name:
        raise SkillsError("missing_skill", "no skill name provided")
    if scope not in ("global", "project"):
        raise SkillsError("invalid_scope", "update scope must be global or project")
    context = resolve_resource_access_context(user_context)
    if not context.is_trusted_local:
        resource_ids = await _installed_skill_resource_ids(
            askill_path,
            name,
            scope=scope,
            project_dir=project_dir,
            backends=None,
        )
        if resource_ids:
            _require_skill_management_access(
                resource_ids,
                user_context=context,
                allow_missing_policy=False,
            )
    args = ["update", name, *_target_scope_flag(scope), "-y"]
    return await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))
