"""Owner-only, private-IPC Session request reconstruction. Never a dispatch entry point."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from config import paths
from core.agent_core.agent.preview import RequestPreview
from core.agent_session_context import session_model_settings
from core.private_fs import ensure_private_directory, is_private
from modules.agents.base import AgentRequest
from storage.resource_access_service import (
    RESOURCE_USER_CONTEXT_METADATA_KEY,
    metadata_with_resource_user_context,
    resource_user_context_from_metadata,
)
from vibe.authorization import (
    AuthorizationContext,
    InstanceAuthorizationError,
    instance_owner_context,
    require_instance_role,
)


def preview_caller() -> dict[str, Any]:
    """Carry this CLI invocation's authority, not a daemon's or the target Session's."""
    authority = require_instance_role(None, "owner")
    if not authority.is_remote:
        return {"kind": "local"}
    metadata = metadata_with_resource_user_context({}, authority)
    return {"kind": "remote", RESOURCE_USER_CONTEXT_METADATA_KEY: metadata[RESOURCE_USER_CONTEXT_METADATA_KEY]}


def require_preview_owner(caller: object) -> AuthorizationContext:
    """Recheck mandatory provenance across IPC. Missing/invalid remote provenance never means local."""
    if caller == {"kind": "local"}:
        # The existing host-local Owner path, behind owner-private control IPC.
        return require_instance_role(instance_owner_context(), "owner")
    if not isinstance(caller, dict) or set(caller) != {"kind", RESOURCE_USER_CONTEXT_METADATA_KEY}:
        raise InstanceAuthorizationError("owner")
    if caller.get("kind") != "remote" or not isinstance(caller.get(RESOURCE_USER_CONTEXT_METADATA_KEY), dict):
        raise InstanceAuthorizationError("owner")
    authority = resource_user_context_from_metadata(caller)
    if authority is None:
        raise InstanceAuthorizationError("owner")
    return require_instance_role(authority, "owner")


def build_preview_request(controller: Any, context: Any) -> AgentRequest:
    """Read the same durable Agent and setting defaults as dispatch, without materializing any of them."""
    from sqlalchemy import select

    from core.services.agent_run_target import session_project_base_from_row
    from storage.models import agent_sessions, scope_settings

    target = (context.platform_specific or {}).get("agent_session_target")
    if not isinstance(target, dict) or target.get("agent_backend") != "vibey":
        raise ValueError("request_preview_requires_vibey")
    session_id = target["id"]
    agent = controller.resolve_vibe_agent_for_context(
        context, override_agent_id=target.get("agent_id"), override_agent_name=target.get("agent_name"), required=True,
    )
    if agent is None or agent.backend != "vibey":
        raise ValueError("request_preview_requires_vibey")
    cwd = target.get("workdir")
    cwd = os.path.abspath(os.path.expanduser(cwd)) if isinstance(cwd, str) and cwd else ""
    if not cwd or not Path(cwd).is_dir():
        # get_cwd()/resolve_agent_run_target() can create a missing workdir, even with create_session=False.
        raise ValueError("request_preview_workdir_unavailable")
    model, effort, explicit = session_model_settings(
        target, fallback_model=agent.model, fallback_reasoning_effort=agent.reasoning_effort,
    )
    project_base = None
    from storage.db import get_cached_sqlite_engine

    with get_cached_sqlite_engine().connect() as conn:
        row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == session_id)).mappings().one_or_none()
        if row is None:
            raise ValueError("request_preview_session_missing")
        row = dict(row)
        row["_scope_workdir"] = conn.execute(
            select(scope_settings.c.workdir).where(scope_settings.c.scope_id == row.get("scope_id"))
        ).scalar_one_or_none()
    project_base = session_project_base_from_row(row, cwd)
    # Bind only this detached context, so shared Skill discovery uses the production project boundary.
    context.platform_specific["agent_run_target"] = {**target, "agent_session_id": session_id, "project_base": project_base}
    return AgentRequest(
        context=context, message="", user_message="", working_path=cwd,
        base_session_id=session_id, composite_session_id=session_id, session_key="",
        vibe_agent_id=agent.id, vibe_agent_name=agent.name, vibe_agent_backend=agent.backend,
        vibe_agent_model=model, vibe_agent_reasoning_effort=effort,
        vibe_agent_model_explicit="model" in explicit, vibe_agent_reasoning_effort_explicit="reasoning_effort" in explicit,
        vibe_agent_system_prompt=agent.system_prompt,
    )


def _private_write(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        if not is_private(path, os.fstat(stream.fileno()).st_mode, fd=stream.fileno()):
            raise PermissionError("request_preview_artifact_not_private")
        stream.write(content)


def export_preview(preview: RequestPreview, *, requested_context_seq: int | None) -> dict[str, Any]:
    """Only this artifact writer has effects. Its return value is safe for CLI stdout and IPC."""
    status = (
        "ready" if preview.status == "ready" else
        "transition_required" if preview.status == "needs_context_transition" else "not_model_ready"
    )
    summary: dict[str, Any] = {
        "kind": "request_preview", "status": status,
        "reason": preview.status, "mode": "current_config_reconstruction", "auth": "unbound",
        "session_id": preview.snapshot.session_id,
        "context_seq": preview.snapshot.context_seq, "requested_context_seq": requested_context_seq,
        "transition_required": preview.transition is not None,
        "missing_tool_results": preview.status == "pending_tools",
        "wire_prepared": preview.wire is not None,
    }
    if preview.transition is not None:
        summary["transition"] = preview.transition
    if preview.wire is not None:
        summary.update(body_bytes=len(preview.wire.body), body_sha256=preview.wire.body_sha256)
    from vibe import __version__

    manifest = {
        "schema_version": 1, "avibe_version": __version__, **summary,
        "limitations": [
            "Current configuration, not historical system/tools/route capture or a running Turn's volatile retry state.",
            "Unconsumed inputs are excluded; no new input or environment block is appended.",
            "Authentication and gateway routing are unbound; this artifact cannot be sent as-is.",
        ],
    }
    if preview.budget is not None:
        manifest["budget"] = asdict(preview.budget)
    if preview.request is not None:
        endpoint = preview.request.endpoint
        manifest["route"] = {"protocol": endpoint.protocol, "model": endpoint.model_id, "source_id": endpoint.source_id}
    # Never serialize ModelRequest, snapshot state, WireRequest headers/URL, or ProviderError text.
    root = paths.get_state_dir() / "request-previews"
    ensure_private_directory(root, repair=False)
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise PermissionError("request_preview_artifact_not_private")
    directory = Path(tempfile.mkdtemp(prefix="preview-", dir=root))
    try:
        directory_mode = directory.lstat().st_mode
        if stat.S_ISLNK(directory_mode) or not stat.S_ISDIR(directory_mode) or not is_private(directory, directory_mode):
            raise PermissionError("request_preview_artifact_not_private")
        _private_write(directory / "manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
        if preview.wire is not None:
            _private_write(directory / "request.body.json", preview.wire.body)
    except BaseException:
        shutil.rmtree(directory)
        raise
    summary.update(artifact_dir=str(directory), manifest_path=str(directory / "manifest.json"))
    if preview.wire is not None:
        summary["request_body_path"] = str(directory / "request.body.json")
    return summary


async def preview_session_request(controller: Any, context: Any, *, context_seq: int | None) -> Mapping[str, Any]:
    """The private controller boundary; caller authority must already be installed for this operation."""
    require_instance_role(None, "owner")
    request = await asyncio.to_thread(build_preview_request, controller, context)
    backend = getattr(getattr(controller, "agent_service", None), "agents", {}).get("vibey")
    if backend is None:
        raise ValueError("request_preview_backend_unavailable")
    session_id = (context.platform_specific or {})["agent_session_target"]["id"]
    preview = await backend.preview_request(session_id, request, context_seq=context_seq)
    return await asyncio.to_thread(export_preview, preview, requested_context_seq=context_seq)
