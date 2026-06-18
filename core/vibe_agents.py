"""Vibe-owned Agent catalog and import helpers."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4

import yaml
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from config import paths
from storage.db import SqliteInvalidationProbe, create_sqlite_engine
from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
from storage.migrations import run_migrations
from storage.models import agent_sessions, agents, run_definitions, scope_settings, state_meta

logger = logging.getLogger(__name__)

DEFAULT_AGENT_NAME = "default"
DEFAULT_AGENT_META_KEY = "default_agent_name"
BUILTIN_DEFAULT_AGENT_METADATA = {"builtin": True, "builtin_default": True, "lock_delete": True}
BUILTIN_BACKEND_ENABLED_META_KEY = "backend_enabled"
SUPPORTED_AGENT_BACKENDS = {"codex", "claude", "opencode"}
_UNSET = object()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_agent_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(name or "").strip().lower()).strip("-_")
    if not normalized:
        raise ValueError("agent name is required")
    return normalized


def validate_agent_backend(backend: str) -> str:
    value = str(backend or "").strip().lower()
    if value not in SUPPORTED_AGENT_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_AGENT_BACKENDS))
        raise ValueError(f"unsupported agent backend: {backend}. Supported backends: {supported}")
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class VibeAgent:
    id: str
    name: str
    normalized_name: str
    backend: str
    description: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    system_prompt: Optional[str] = None
    enabled: bool = True
    source: str = "user"
    source_ref: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentImportCandidate:
    name: str
    backend: str
    description: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    system_prompt: Optional[str] = None
    source: str = "import"
    source_ref: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentImportResult:
    imported: list[VibeAgent]
    skipped: list[dict[str, Any]]


class VibeAgentStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or paths.get_sqlite_state_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path is None:
            ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
        else:
            run_migrations(self.db_path)
        self.engine = create_sqlite_engine(self.db_path)
        self._probe = SqliteInvalidationProbe(self.engine)

    def close(self) -> None:
        self._probe.close()
        self.engine.dispose()

    def maybe_reload(self) -> bool:
        return self._probe.has_external_write()

    def list_agents(self, *, include_disabled: bool = True) -> list[VibeAgent]:
        with self.engine.connect() as conn:
            stmt = select(agents).order_by(agents.c.name)
            if not include_disabled:
                stmt = stmt.where(agents.c.enabled == 1)
            rows = conn.execute(stmt).mappings()
            return [self._from_row(row) for row in rows]

    def get(self, name: str) -> Optional[VibeAgent]:
        normalized = normalize_agent_name(name)
        with self.engine.connect() as conn:
            row = conn.execute(
                select(agents).where(agents.c.normalized_name == normalized).limit(1)
            ).mappings().first()
            return self._from_row(row) if row else None

    def require(self, name: str) -> VibeAgent:
        agent = self.get(name)
        if agent is None:
            raise ValueError(f"agent '{name}' not found")
        return agent

    def require_enabled(self, name: str) -> VibeAgent:
        agent = self.require(name)
        if not agent.enabled:
            raise ValueError(f"agent '{agent.name}' is disabled")
        return agent

    def create(
        self,
        *,
        name: str,
        backend: str,
        description: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        system_prompt: Optional[str] = None,
        source: str = "user",
        source_ref: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        enabled: bool = True,
    ) -> VibeAgent:
        normalized = normalize_agent_name(name)
        now = _utc_now_iso()
        agent = VibeAgent(
            id=uuid4().hex[:12],
            name=str(name).strip(),
            normalized_name=normalized,
            backend=validate_agent_backend(backend),
            description=_clean_optional(description),
            model=_clean_optional(model),
            reasoning_effort=_clean_optional(reasoning_effort),
            system_prompt=_clean_optional(system_prompt),
            enabled=bool(enabled),
            source=str(source or "user"),
            source_ref=_clean_optional(source_ref),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        try:
            with self.engine.begin() as conn:
                conn.execute(agents.insert().values(**self._values(agent)))
        except IntegrityError as exc:
            raise ValueError(f"agent '{name}' already exists") from exc
        return agent

    def update(
        self,
        name: str,
        *,
        description: Any = _UNSET,
        model: Any = _UNSET,
        reasoning_effort: Any = _UNSET,
        system_prompt: Any = _UNSET,
        metadata: Any = _UNSET,
        enabled: Any = _UNSET,
    ) -> VibeAgent:
        existing = self.require(name)
        values: dict[str, Any] = {"updated_at": _utc_now_iso()}
        if description is not _UNSET:
            values["description"] = _clean_optional(description)
        if model is not _UNSET:
            values["model"] = _clean_optional(model)
        if reasoning_effort is not _UNSET:
            values["reasoning_effort"] = _clean_optional(reasoning_effort)
        if system_prompt is not _UNSET:
            values["system_prompt"] = _clean_optional(system_prompt)
        if metadata is not _UNSET:
            values["metadata_json"] = _json_dumps(dict(metadata or {}))
        if enabled is not _UNSET:
            values["enabled"] = 1 if bool(enabled) else 0
        with self.engine.begin() as conn:
            conn.execute(agents.update().where(agents.c.id == existing.id).values(**values))
        return self.require(name)

    def set_enabled(self, name: str, enabled: bool) -> VibeAgent:
        return self.update(name, enabled=enabled)

    def remove(self, name: str) -> bool:
        agent = self.get(name)
        if agent is None:
            return False
        if is_builtin_default_agent(agent):
            raise ValueError(f"agent '{agent.name}' is built in and cannot be deleted")
        normalized = agent.normalized_name
        with self.engine.begin() as conn:
            result = conn.execute(agents.delete().where(agents.c.normalized_name == normalized))
            return bool(result.rowcount)

    def reference_counts(self, name: str) -> dict[str, int]:
        normalized = normalize_agent_name(name)
        agent = self.get(normalized)
        if agent is None:
            return {}
        with self.engine.connect() as conn:
            scope_count = conn.execute(
                select(scope_settings.c.scope_id).where(scope_settings.c.agent_name == agent.name)
            ).fetchall()
            session_count = conn.execute(
                select(agent_sessions.c.id).where(agent_sessions.c.agent_name == agent.name)
            ).fetchall()
            definition_count = conn.execute(
                select(run_definitions.c.id)
                .where(run_definitions.c.agent_name == agent.name)
                .where(run_definitions.c.deleted_at.is_(None))
            ).fetchall()
        return {
            "scopes": len(scope_count),
            "sessions": len(session_count),
            "definitions": len(definition_count),
        }

    def import_candidates(self, candidates: Iterable[AgentImportCandidate]) -> AgentImportResult:
        imported: list[VibeAgent] = []
        skipped: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                if self.get(candidate.name):
                    skipped.append({"name": candidate.name, "reason": "name_conflict"})
                    continue
                imported.append(
                    self.create(
                        name=candidate.name,
                        backend=candidate.backend,
                        description=candidate.description,
                        model=candidate.model,
                        reasoning_effort=candidate.reasoning_effort,
                        system_prompt=candidate.system_prompt,
                        source=candidate.source,
                        source_ref=candidate.source_ref,
                        metadata=candidate.metadata,
                    )
                )
            except Exception as exc:
                skipped.append({"name": candidate.name, "reason": "invalid", "error": str(exc)})
        return AgentImportResult(imported=imported, skipped=skipped)

    def ensure_default_agent(self, *, backend: str = "claude") -> VibeAgent:
        existing = self.get(DEFAULT_AGENT_NAME)
        if existing:
            self.set_default_agent_name(existing.name)
            return existing
        agent = self.create(
            name=DEFAULT_AGENT_NAME,
            backend=backend,
            description="Default avibe agent.",
            source="builtin",
            metadata={"builtin": True},
            enabled=True,
        )
        self.set_default_agent_name(agent.name)
        return agent

    def ensure_builtin_default_agent(self, *, backend: str, name: str | None = None) -> VibeAgent:
        backend = validate_agent_backend(backend)
        agent_name = str(name or backend).strip()
        metadata = dict(BUILTIN_DEFAULT_AGENT_METADATA)
        metadata["backend"] = backend
        existing = self.get(agent_name)
        if existing:
            if existing.backend != backend:
                raise ValueError(
                    f"agent '{agent_name}' already exists with backend '{existing.backend}', "
                    f"cannot use it as the built-in default for '{backend}'"
                )
            if not is_builtin_default_agent(existing):
                return existing
            merged = {**existing.metadata, **metadata}
            if existing.source != "builtin" or existing.metadata != merged:
                return self.update(existing.name, metadata=merged)
            return existing
        return self.create(
            name=agent_name,
            backend=backend,
            description=f"Default Agent for the {backend} backend.",
            source="builtin",
            metadata=metadata,
            enabled=True,
        )

    def sync_builtin_default_agent(self, *, backend: str, backend_enabled: bool, name: str | None = None) -> VibeAgent:
        backend = validate_agent_backend(backend)
        agent = self.ensure_builtin_default_agent(backend=backend, name=name)
        if not is_builtin_default_agent(agent):
            return agent

        previous_backend_enabled = agent.metadata.get(BUILTIN_BACKEND_ENABLED_META_KEY)
        metadata = {**agent.metadata, BUILTIN_BACKEND_ENABLED_META_KEY: bool(backend_enabled)}
        should_enable = bool(backend_enabled) and previous_backend_enabled is not True
        should_disable = not bool(backend_enabled) and agent.enabled
        updates: dict[str, Any] = {}
        if metadata != agent.metadata:
            updates["metadata"] = metadata
        if should_enable:
            updates["enabled"] = True
        elif should_disable:
            updates["enabled"] = False
        if updates:
            return self.update(agent.name, **updates)
        return agent

    def ensure_builtin_default_agents(
        self,
        backends: Iterable[str],
    ) -> list[VibeAgent]:
        ensured: list[VibeAgent] = []
        enabled_backends: list[str] = []
        for backend in backends:
            normalized_backend = validate_agent_backend(backend)
            if normalized_backend not in enabled_backends:
                enabled_backends.append(normalized_backend)
        enabled_backend_set = set(enabled_backends)
        for backend in enabled_backends:
            try:
                ensured.append(self.sync_builtin_default_agent(backend=backend, backend_enabled=True))
            except ValueError as exc:
                logger.warning("Skipping built-in default Agent for backend %s: %s", backend, exc)
        with self.engine.connect() as conn:
            rows = conn.execute(select(agents)).mappings().all()
        for row in rows:
            agent = self._from_row(row)
            if (
                is_builtin_default_agent(agent)
                and agent.backend not in enabled_backend_set
            ):
                self.sync_builtin_default_agent(backend=agent.backend, backend_enabled=False, name=agent.name)
        default_name = self.get_default_agent_name()
        default_agent = self.get(default_name) if default_name else None
        enabled_ensured = [agent for agent in ensured if agent.enabled]
        if (default_agent is None or not default_agent.enabled) and enabled_ensured:
            self.set_default_agent_name(enabled_ensured[0].name)
        return ensured

    def get_builtin_default_agent_for_backend(self, backend: str, *, enabled_only: bool = True) -> Optional[VibeAgent]:
        backend = validate_agent_backend(backend)
        for candidate in (backend, DEFAULT_AGENT_NAME):
            agent = self.get(candidate)
            if (
                agent
                and agent.backend == backend
                and is_builtin_default_agent(agent)
                and (agent.enabled or not enabled_only)
            ):
                return agent
        with self.engine.connect() as conn:
            rows = conn.execute(select(agents).where(agents.c.backend == backend).order_by(agents.c.name)).mappings()
            for row in rows:
                agent = self._from_row(row)
                if is_builtin_default_agent(agent) and (agent.enabled or not enabled_only):
                    return agent
        return None

    def get_default_agent_name(self) -> Optional[str]:
        with self.engine.connect() as conn:
            value = conn.execute(
                select(state_meta.c.value_json).where(state_meta.c.key == DEFAULT_AGENT_META_KEY).limit(1)
            ).scalar_one_or_none()
        payload = _json_loads(value, None)
        return str(payload).strip() if payload else None

    def set_default_agent_name(self, name: str) -> None:
        agent = self.require_enabled(name)
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            conn.execute(state_meta.delete().where(state_meta.c.key == DEFAULT_AGENT_META_KEY))
            conn.execute(
                state_meta.insert().values(
                    key=DEFAULT_AGENT_META_KEY,
                    value_json=_json_dumps(agent.name),
                    updated_at=now,
                )
            )

    def get_default_agent(self, *, enabled_only: bool = True) -> Optional[VibeAgent]:
        name = self.get_default_agent_name()
        if name:
            agent = self.get(name)
            if agent is not None and (agent.enabled or not enabled_only):
                return agent
        fallback = self.get(DEFAULT_AGENT_NAME)
        if fallback is not None and (fallback.enabled or not enabled_only):
            return fallback
        if enabled_only:
            agents_list = self.list_agents(include_disabled=False)
            return agents_list[0] if agents_list else None
        return None

    @staticmethod
    def _from_row(row: Any) -> VibeAgent:
        return VibeAgent(
            id=row["id"],
            name=row["name"],
            normalized_name=row["normalized_name"],
            backend=row["backend"],
            description=row["description"],
            model=row["model"],
            reasoning_effort=row["reasoning_effort"],
            system_prompt=row["system_prompt"],
            enabled=bool(row["enabled"]),
            source=row["source"],
            source_ref=row["source_ref"],
            metadata=_json_loads(row["metadata_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _values(agent: VibeAgent) -> dict[str, Any]:
        return {
            "id": agent.id,
            "name": agent.name,
            "normalized_name": agent.normalized_name,
            "description": agent.description,
            "backend": agent.backend,
            "model": agent.model,
            "reasoning_effort": agent.reasoning_effort,
            "system_prompt": agent.system_prompt,
            "enabled": 1 if agent.enabled else 0,
            "source": agent.source,
            "source_ref": agent.source_ref,
            "metadata_json": _json_dumps(agent.metadata),
            "created_at": agent.created_at,
            "updated_at": agent.updated_at,
        }


def parse_agent_file(path: Path, *, backend: str) -> AgentImportCandidate:
    backend = validate_agent_backend(backend)
    raw = path.read_text(encoding="utf-8")
    header: dict[str, Any] = {}
    body = raw.strip()
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            header = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()
    name = str(header.get("name") or path.stem).strip()
    description = header.get("description")
    model = header.get("model")
    reasoning_effort = header.get("reasoning_effort") or header.get("reasoningEffort")
    metadata = {
        key: value
        for key, value in header.items()
        if key not in {"name", "description", "model", "reasoning_effort", "reasoningEffort"}
    }
    return AgentImportCandidate(
        name=name,
        backend=backend,
        description=str(description).strip() if description else None,
        model=str(model).strip() if model else None,
        reasoning_effort=str(reasoning_effort).strip() if reasoning_effort else None,
        system_prompt=body or None,
        source="file",
        source_ref=str(path),
        metadata=metadata,
    )


def is_builtin_default_agent(agent: VibeAgent) -> bool:
    return bool(agent.metadata.get("builtin_default") or agent.metadata.get("lock_delete"))


def iter_global_agent_files(source: str) -> list[tuple[Path, str]]:
    source_key = str(source or "").strip().lower()
    home = Path.home()
    if source_key == "claude":
        return [(path, "claude") for path in sorted((home / ".claude" / "agents").glob("*.md"))]
    if source_key == "codex":
        search_dirs = [home / ".codex" / "agents"]
        return [(path, "codex") for directory in search_dirs for path in sorted(directory.glob("*.md"))]
    if source_key == "opencode":
        search_dirs = [
            home / ".config" / "opencode" / "agent",
            home / ".config" / "opencode" / "agents",
        ]
        return [(path, "opencode") for directory in search_dirs for path in sorted(directory.glob("*.md"))]
    raise ValueError("--from must be one of: claude, codex, opencode")


def _clean_optional(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
