"""Skill payload construction and bounded, optional runtime recording."""

from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Sequence
import uuid

from core.caller_context import caller_context_from_env
from core.managed_skills import ManagedSkill, SKILL_PROJECT_BASE_ENV, SKILL_WORKING_DIR_ENV, catalog_page
from storage import skill_observability as store
from vibe import __version__

logger = logging.getLogger(__name__)
MAX_PENDING_OBSERVATIONS = 128
_CLI_HEALTH: Counter[str] = Counter()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def skill_descriptor(skill: ManagedSkill) -> dict[str, Any]:
    source = {0: "builtin", 1: "project", 2: "global"}[skill.priority[0]]
    identity = [1, "builtin", skill.name] if source == "builtin" else [1, source, str(skill.directory), skill.name]
    return {
        "skill_key": _digest(identity),
        "skill_name": skill.name,
        "source_kind": source,
        "descriptor_sha256": _digest([1, skill.name, skill.description, skill.disable_model_invocation]),
    }


def catalog_result(
    skills: Sequence[ManagedSkill],
    *,
    page: int = 1,
    entry_point: str = "cli_list",
    error_code: str | None = None,
) -> dict[str, Any]:
    entries, next_page = catalog_page(skills, page) if error_code is None else ([], None)
    rows = [{**skill_descriptor(skill), "position": index} for index, skill in enumerate(entries, 1)]
    return {
        "entry_point": entry_point,
        "outcome": "failure" if error_code else "success",
        "error_code": error_code,
        "page": page if type(page) is int and page >= 0 else 0,
        "has_more": next_page is not None,
        "catalog_digest": _digest(rows) if not error_code else None,
        "entries": rows,
    }


def load_result(
    name: str,
    skill: ManagedSkill | None,
    *,
    duration_ms: int,
    error_code: str | None = None,
) -> dict[str, Any]:
    revision = None
    body_bytes = None
    if skill is not None:
        descriptor = skill_descriptor(skill)
        if skill.body is not None:
            revision = _digest([1, skill.name, skill.description, skill.disable_model_invocation, skill.body])
            body_bytes = len(skill.body.encode("utf-8"))
    else:
        valid_name = isinstance(name, str) and len(name) <= 64 and store.NAME_RE.fullmatch(name)
        project = os.environ.get(SKILL_PROJECT_BASE_ENV) or os.environ.get(SKILL_WORKING_DIR_ENV) or os.getcwd()
        descriptor = {
            "skill_key": _digest([1, "unresolved", str(Path(project).resolve()), name]) if valid_name else None,
            "skill_name": name if valid_name else None,
            "source_kind": "unresolved",
            "descriptor_sha256": None,
        }
        if not valid_name:
            error_code = "invalid_name"
    return {
        **descriptor,
        "skill_revision": revision,
        "outcome": "failure" if error_code else "success",
        "error_code": error_code,
        "duration_ms": duration_ms,
        "body_bytes": body_bytes,
    }


def observation(
    event_type: str,
    content: dict,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    trigger_kind: str = "unknown",
    backend: str | None = None,
) -> dict[str, Any]:
    return {
        "id": "evt_skill_" + uuid.uuid4().hex,
        "session_id": session_id,
        "turn_id": turn_id,
        "event_type": event_type,
        "content": content,
        "metadata": {
            "schema_version": 1,
            "observed_at": store.timestamp(store.utc_now()),
            "observation_channel": "runtime_prompt" if content.get("entry_point") == "runtime_prompt" else "avibe_cli",
            "trigger_kind": trigger_kind,
            "backend": backend,
            "model": None,
            "avibe_version": __version__,
        },
    }


def submit_cli(event_type: str, content: dict) -> None:
    """Never change command stdout or its exit outcome for optional telemetry."""
    try:
        from vibe.internal_client import record_skill_observation_sync

        caller = caller_context_from_env()
        event = observation(
            event_type,
            content,
            session_id=caller.session_id if caller else None,
            backend=caller.backend if caller else None,
            trigger_kind="standalone" if caller is None and sys.stdin.isatty() else "unknown",
        )
        result = record_skill_observation_sync(event)
        status = result.get("status", "dropped")
        _CLI_HEALTH[status] += 1
        if status not in {"queued", "disabled"}:
            logger.info("Skill observation not queued: %s", status if status == "dropped" else "rejected")
    except Exception:
        _CLI_HEALTH["dropped"] += 1
        logger.info("Skill observation unavailable; command result preserved")


def finish_cli_load(name: str, skill: ManagedSkill | None, started_at: float, error_code: str | None) -> None:
    try:
        submit_cli(
            "skill.load_result", load_result(name, skill, duration_ms=elapsed_ms(started_at), error_code=error_code)
        )
    except Exception:
        _CLI_HEALTH["dropped"] += 1
        logger.info("Skill observation construction failed")


def finish_cli_catalog(skills: Sequence[ManagedSkill], page: int, error_code: str | None) -> None:
    try:
        submit_cli("skill.catalog_result", catalog_result(skills, page=page, error_code=error_code))
    except Exception:
        _CLI_HEALTH["dropped"] += 1
        logger.info("Skill catalog observation construction failed")


def accept_catalog(controller: Any, context: Any, candidate: dict | None, *, backend: str | None = None) -> None:
    """Called only after positive native acceptance, never from a renderer."""
    if not candidate:
        return
    recorder = getattr(controller, "skill_observability", None)
    if recorder is None:
        return
    try:
        payload = getattr(context, "platform_specific", None) or {}
        event = observation(
            "skill.catalog_result",
            candidate,
            session_id=payload.get("agent_session_id"),
            turn_id=payload.get("turn_token"),
            backend=backend,
        )
        recorder.enqueue(event, trusted_runtime=True)
    except Exception:
        logger.warning("Skill catalog observation dropped after native acceptance")


def collection_enabled() -> bool:
    from config.v2_config import V2Config

    try:
        config = V2Config.load()
    except Exception:
        logger.info("Skill observation collection disabled: config unavailable")
        return False
    if any(
        any(marker in str(warning) for marker in ("runtime", "recovery defaults", "could not be recovered"))
        for warning in getattr(config, "load_warnings", ())
    ):
        return False
    return config.runtime.skill_observability_enabled is True


class SkillObservationRecorder:
    """One bounded queue and one serial writer, owned by the internal server."""

    def __init__(self, *, engine=None, enabled=collection_enabled):
        self.engine = engine
        self.enabled = enabled
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_PENDING_OBSERVATIONS)
        self.worker: asyncio.Task | None = None
        self.closed = False
        self.counts: Counter[str] = Counter(
            dict.fromkeys(
                ("queued", "accepted", "duplicate", "disabled", "rejected", "dropped", "uncorrelated"),
                0,
            )
        )
        self.started_at = store.timestamp(store.utc_now())

    def enqueue(self, event: dict, *, trusted_runtime: bool = False) -> str:
        if self.closed or self.queue.full():
            self.counts["dropped"] += 1
            return "dropped"
        self.queue.put_nowait((event, trusted_runtime))
        if self.worker is None:
            self.worker = asyncio.create_task(self._drain(), name="skill-observations")
        self.counts["queued"] += 1
        return "queued"

    def _write(self, event: dict, trusted_runtime: bool) -> str:
        from storage.db import get_cached_sqlite_engine

        engine = self.engine if self.engine is not None else get_cached_sqlite_engine()
        with engine.begin() as conn:
            return store.record(conn, event, enabled=self.enabled, trusted_runtime=trusted_runtime)

    async def _drain(self) -> None:
        while True:
            event, trusted = await self.queue.get()
            try:
                result = await asyncio.to_thread(self._write, event, trusted)
                self.counts[result] += 1
                if result == "accepted" and event["session_id"] is None:
                    self.counts["uncorrelated"] += 1
            except store.ObservationError as exc:
                self.counts["rejected"] += 1
                logger.warning("Skill observation rejected: %s", exc)
            except Exception:
                self.counts["dropped"] += 1
                logger.warning("Skill observation storage unavailable")
            finally:
                self.queue.task_done()

    def health(self) -> dict:
        return {"started_at": self.started_at, "pending": self.queue.qsize(), **self.counts}

    async def close(self) -> None:
        self.closed = True
        # Drop queued optional work; join at most the one in-flight DB write.
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
            self.counts["dropped"] += 1
        await self.queue.join()
        if self.worker is not None:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass


def elapsed_ms(started_at: float) -> int:
    return min(86_400_000, max(0, int((time.monotonic() - started_at) * 1000)))
