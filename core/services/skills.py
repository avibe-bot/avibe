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
import json
import logging
import os
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


class SkillsError(Exception):
    """A failure with a stable ``code`` the route layer maps to HTTP/i18n."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


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


# --- public API -----------------------------------------------------------


async def list_skills(
    askill_path: str,
    *,
    scope: str = "all",
    project_dir: Optional[str] = None,
    backends: Optional[list[str]] = None,
) -> dict[str, Any]:
    """List installed skills. ``scope`` is ``all`` | ``global`` | ``project``.

    Project-scoped lists run with ``cwd=project_dir`` so askill resolves the
    repo's ``.agents/skills``. Each item carries description / version / tags /
    source / installSource / timestamps natively (askill v0.1.13+).
    """
    args = ["list", *_list_scope_flag(scope), *_agent_flags(backends)]
    return await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))


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
    return result


async def remove_skill(
    askill_path: str,
    name: str,
    *,
    scope: str = "project",
    project_dir: Optional[str] = None,
    backends: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Remove an installed skill, optionally from specific backends only.

    Maps to ``askill remove <name> [-g] [-a <agent>...]``.
    """
    if not name:
        raise SkillsError("missing_skill", "no skill name provided")
    if scope not in ("global", "project"):
        raise SkillsError("invalid_scope", "remove scope must be global or project")
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
) -> dict[str, Any]:
    """Update one installed skill. Maps to ``askill update <name> [-g] -y``."""
    if not name:
        raise SkillsError("missing_skill", "no skill name provided")
    args = ["update", name, *_target_scope_flag(scope), "-y"]
    return await _run_askill(askill_path, args, cwd=_cwd_for(scope, project_dir))
