"""Read-only snapshot of currently-running agent instances.

This aggregator runs INSIDE the controller process (reachable only from the
controller's asyncio loop via ``core/internal_server.py``) because every
liveness source it reads is controller in-memory state:

- Claude: ``controller.claude_sessions`` (composite_key -> SDK client),
  ``claude_active_sessions`` (active turn set), ``session_last_activity``.
- Codex: ``CodexAgent._session_mgr`` / ``_turn_registry`` / ``_transports``
  (one transport/pid per working dir, shared by many sessions).
- OpenCode: ``OpenCodeAgent._active_requests`` (no OS subprocess / pid).
- Orphans: the persisted Claude process registry (``claude_processes.json``)
  for ``owner == "session"`` processes that no longer have a live SDK client.

Durations are computed HERE (the controller owns the ``time.monotonic()``
baselines; raw monotonic values are meaningless in the UI process).

Display metadata (platform / scope / title / workdir) is enriched from the
SQLite ``agent_sessions`` + ``scopes`` tables, joined by
``session_anchor == base_session_id``. ``agent_sessions.agent_status`` is NOT
trusted as a liveness source: it is only written for workbench sessions, so IM
truth comes solely from the in-memory registries above.

The snapshot is strictly read-only; it never mutates sessions, transports, the
process registry, or eviction state.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from core.controller import Controller

logger = logging.getLogger(__name__)


def _safe_call(fn, default):
    """Call ``fn`` tolerating ``RuntimeError`` from concurrent mutation.

    Like ``_safe_items`` but for callables that iterate live registries
    internally (e.g. ``CodexSessionManager.all_base_sessions`` unions three
    lock-free dicts that the controller loop may mutate while we run in a worker
    thread). Retries a few times, then returns ``default``.
    """
    for _ in range(3):
        try:
            return fn()
        except RuntimeError:
            continue
    return default


def _safe_items(mapping: Any) -> list:
    """``list(mapping.items())`` tolerant of concurrent mutation.

    The snapshot runs in a worker thread (``asyncio.to_thread``) while the
    controller loop may still mutate these registries, so a plain
    ``list(d.items())`` can raise ``RuntimeError: dictionary changed size during
    iteration``. Reuses ``_safe_call``'s retry-then-default behavior.
    """
    if not hasattr(mapping, "items"):
        return []
    return _safe_call(lambda: list(mapping.items()), [])


def _base_from_composite(composite_key: str) -> str:
    """Recover ``base_session_id`` from a Claude composite key.

    Composite keys are ``f"{base_session_id}:{working_path}"``; subagent bases
    themselves contain a colon (``{platform}_{thread}:{agent_name}``), so a
    naive ``split(":")[0]`` is wrong. ``working_path`` is an absolute path with
    no colon, so the base is everything before the LAST colon.
    """
    base, sep, _workdir = composite_key.rpartition(":")
    return base if sep else composite_key


def _split_scope_id(scope_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Parse ``<platform>::<scope_type>::<native_id>`` (see make_scope_id) into
    ``(platform, scope_type)``; either may be ``None`` when absent."""
    if not scope_id:
        return None, None
    parts = scope_id.split("::")
    platform = parts[0] or None
    scope_type = parts[1] if len(parts) >= 2 else None
    return platform, scope_type


def _make_row(
    *,
    backend: str,
    state: str,
    base_session_id: Optional[str] = None,
    composite_key: Optional[str] = None,
    workdir: Optional[str] = None,
    pid: Optional[int] = None,
    pid_shared: bool = False,
    native_session_id: Optional[str] = None,
    model: Optional[str] = None,
    elapsed_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """Build one normalized running-agent row.

    ``state`` is one of ``active`` (turn in flight), ``idle`` (connected but no
    active turn), ``orphan`` (process in registry with no live session).
    ``elapsed_seconds`` reads as "busy for" on active rows (seconds since the
    current turn went active) and "idle for" on idle/orphan rows (seconds since
    the last activity); ``None`` when the baseline is unknown.
    """
    return {
        "backend": backend,
        "state": state,
        "base_session_id": base_session_id,
        "composite_key": composite_key,
        "workdir": workdir,
        "pid": pid,
        "pid_shared": pid_shared,
        "native_session_id": native_session_id,
        "model": model,
        "elapsed_seconds": (round(elapsed_seconds, 1) if isinstance(elapsed_seconds, (int, float)) else None),
        # Filled by _enrich_from_db when a matching agent_sessions row exists.
        "session_id": None,
        "title": None,
        "platform": None,
        "scope_type": None,
        "scope_display_name": None,
        "trigger_source": None,
        "agent_name": None,
        "openable_in_chat": False,
    }


def _collect_claude(
    controller: "Controller", now: float, seen_native: dict[str, Optional[int]], seen_pids: set[int]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        from modules.agents.claude_process_reaper import get_claude_client_pid
    except Exception:  # noqa: BLE001
        get_claude_client_pid = lambda _client: None  # type: ignore[assignment]

    sessions = getattr(controller, "claude_sessions", {}) or {}
    active = getattr(controller, "claude_active_sessions", set()) or set()
    last_activity = getattr(controller, "session_last_activity", {}) or {}
    turn_started = getattr(controller, "session_turn_started", {}) or {}

    for composite_key, client in _safe_items(sessions):
        # composite_key is ``{base}:{abs_workdir}``; split once for both halves.
        ck_base, ck_sep, ck_workdir = composite_key.rpartition(":")
        base = getattr(client, "_vibe_runtime_base_session_id", None) or (ck_base if ck_sep else composite_key)
        native = getattr(client, "_vibe_native_session_id", None)
        model = getattr(client, "_vibe_current_model", None)
        pid = get_claude_client_pid(client)
        is_active = composite_key in active
        # Active rows report turn duration from the idle→active baseline (NOT
        # ``session_last_activity``, which is bumped on every streamed event and
        # would read as seconds-since-last-chunk); idle rows report idle time from
        # last activity. Fall back to last-activity if the turn baseline is missing.
        if is_active:
            ts = turn_started.get(composite_key)
            if not isinstance(ts, (int, float)):
                ts = last_activity.get(composite_key)
            elapsed = (now - ts) if isinstance(ts, (int, float)) else None
        else:
            la = last_activity.get(composite_key)
            elapsed = (now - la) if isinstance(la, (int, float)) else None
        rows.append(
            _make_row(
                backend="claude",
                state="active" if is_active else "idle",
                base_session_id=base,
                composite_key=composite_key,
                workdir=(ck_workdir or None) if ck_sep else None,
                pid=pid,
                native_session_id=native,
                model=model,
                elapsed_seconds=elapsed,
            )
        )
        if native:
            # Map native id → the live client's pid (None when unresolved) so the
            # orphan scan can tell this live process apart from an older one that
            # leaked on reconnect (same native id, different pid). A known pid wins
            # over a None so an unresolved duplicate can't mask it.
            key = str(native)
            if isinstance(pid, int):
                seen_native[key] = pid
            else:
                seen_native.setdefault(key, None)
        if isinstance(pid, int):
            seen_pids.add(pid)
    return rows


def _collect_codex(controller: "Controller") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    agent = _get_agent(controller, "codex")
    if agent is None:
        return rows
    session_mgr = getattr(agent, "_session_mgr", None)
    turn_registry = getattr(agent, "_turn_registry", None)
    transports = getattr(agent, "_transports", {}) or {}
    if session_mgr is None:
        return rows

    # ``all_base_sessions`` unions three lock-free dicts internally, so guard it
    # against concurrent mutation (we run in a worker thread, §to_thread).
    base_ids = list(_safe_call(session_mgr.all_base_sessions, []))
    # Resolve each base's cwd once, then count sessions per cwd (so the UI can
    # flag a pid shared across sessions) — avoids a second get_cwd pass.
    entries: list[tuple[str, Optional[str], bool, Any]] = []
    cwd_session_count: dict[str, int] = {}
    for base in base_ids:
        cwd = session_mgr.get_cwd(base)
        active_turn = turn_registry.get_active_turn(base) if turn_registry is not None else None
        # A request already holds the Workbench/runtime turn while ``turn/start`` is
        # in flight, but ``get_active_turn`` stays empty until the turn id finalizes.
        # Treat that pending-start window as active too — otherwise the UI offers
        # Disconnect and the idle ``_end_codex`` path bypasses the canonical stop /
        # terminal-result release and can leave the runtime gate stuck.
        has_pending = (
            bool(turn_registry.has_pending_turn_start(base))
            if turn_registry is not None and hasattr(turn_registry, "has_pending_turn_start")
            else False
        )
        is_active = bool(active_turn) or has_pending
        transport = transports.get(cwd) if cwd else None
        # A transport object can outlive its app-server when the process exits out
        # of band (crash / reader-task failure): it lingers in ``_transports`` with
        # ``is_alive`` False until a later cleanup removes it. Treat a dead transport
        # as no live transport so it can't surface as a phantom idle row.
        if transport is not None and not getattr(transport, "is_alive", True):
            transport = None
        # Idle eviction drops the app-server transport but preserves cwd/session
        # mappings for resume bookkeeping. Such bases are not live and must not
        # appear as phantom idle rows; keep only a transport-backed base or a still
        # active (or pending-start) turn that needs to remain visible.
        if transport is None and not is_active:
            continue
        entries.append((base, cwd, is_active, transport))
        if cwd and transport is not None:
            cwd_session_count[cwd] = cwd_session_count.get(cwd, 0) + 1

    for base, cwd, is_active, transport in entries:
        pid = getattr(transport, "pid", None) if transport is not None else None
        # No per-session elapsed for codex: ``_transport_last_activity`` is keyed
        # by cwd (shared across every session on that transport) and is touched on
        # every streaming event, so it reflects neither this session's turn
        # duration nor its idle time. Report ``None`` rather than a misleading
        # value; the row still shows backend / state / pid_shared.
        rows.append(
            _make_row(
                backend="codex",
                state="active" if is_active else "idle",
                base_session_id=base,
                workdir=cwd,
                pid=pid,
                pid_shared=bool(cwd and transport is not None and cwd_session_count.get(cwd, 0) > 1),
                elapsed_seconds=None,
            )
        )
    return rows


def _collect_opencode(controller: "Controller") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    agent = _get_agent(controller, "opencode")
    if agent is None:
        return rows
    # OpenCode has no owned subprocess (HTTP server + poll loop), so no pid.
    # ``_active_requests`` only holds IN-FLIGHT turn tasks (popped in a finally
    # once the turn settles), so OpenCode only ever surfaces as ``active`` here —
    # connected-but-idle OpenCode sessions are not represented (D2 is honored for
    # claude/codex; OpenCode has no idle-session registry to enumerate).
    active_requests = getattr(agent, "_active_requests", {}) or {}
    for base, task in _safe_items(active_requests):
        if bool(getattr(task, "done", lambda: False)()):
            continue  # finishing — being popped; not a live turn
        rows.append(
            _make_row(
                backend="opencode",
                state="active",
                base_session_id=base,
            )
        )
    return rows


def _collect_orphans(seen_native: dict[str, Optional[int]], seen_pids: set[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        from modules.agents.claude_process_reaper import (
            AVIBE_CLAUDE_SESSION_OWNER,
            _load_owned_process_registry,
            _process_ages,
            _process_start_time,
        )
    except Exception:  # noqa: BLE001
        return rows
    try:
        registry = _load_owned_process_registry()
    except Exception:  # noqa: BLE001
        logger.debug("running_agents: failed to read process registry", exc_info=True)
        return rows

    # Filter to session-owned registry rows not already backed by a live client.
    # The registry accumulates stale rows (the reaper prunes lazily), so an entry
    # alone proves nothing — we must verify the process is genuinely alive. A
    # missing/non-numeric ``started_at`` means we cannot prove identity, so we
    # skip it rather than risk a false orphan from pid reuse.
    candidates: list[tuple[Any, int, Optional[str], float]] = []
    for record in registry:
        if getattr(record, "owner", None) != AVIBE_CLAUDE_SESSION_OWNER:
            continue
        native = getattr(record, "native_session_id", None)
        record_pid = getattr(record, "pid", None)
        if native and str(native) in seen_native:
            owner_pid = seen_native[str(native)]
            # Skip only when this record IS the live client's own process, or its
            # pid is unresolved so we can't tell them apart. A live client with the
            # same native id but a DIFFERENT pid means an older process leaked on
            # reconnect — let it fall through to the pid/start-time verification so
            # it can still surface (and be killed) as an orphan.
            if owner_pid is None or owner_pid == record_pid:
                continue
        if not isinstance(record_pid, int) or record_pid in seen_pids:
            continue
        recorded = getattr(record, "started_at", None)
        if not isinstance(recorded, (int, float)):
            continue  # identity unprovable — don't risk a false orphan
        candidates.append((record, record_pid, native, float(recorded)))

    if not candidates:
        return rows

    # ONE batched ``ps`` to drop dead/stale pids (the common case: e.g. 88 stale
    # dead entries collapse to zero here with a single subprocess call). Only the
    # few survivors get the precise per-pid start-time identity check below, so
    # we never fan out N blocking ``ps`` calls on the controller loop.
    ages = _process_ages({pid for _, pid, _, _ in candidates})
    for record, record_pid, native, recorded in candidates:
        if record_pid not in ages:
            continue  # not alive — stale registry entry, not a leak
        current_started_at = _process_start_time(record_pid)
        if current_started_at is None:
            continue
        if abs(current_started_at - recorded) >= 1.0:
            continue  # pid was reused by a different process
        rows.append(
            _make_row(
                backend="claude",
                state="orphan",
                pid=record_pid,
                native_session_id=native,
                elapsed_seconds=ages.get(record_pid),
            )
        )
    return rows


def _get_agent(controller: "Controller", name: str):
    service = getattr(controller, "agent_service", None)
    if service is None:
        return None
    agents = getattr(service, "agents", {}) or {}
    return agents.get(name)


def _enrich_from_db(rows: list[dict[str, Any]]) -> None:
    """Best-effort: fill platform/scope/title/session_id from agent_sessions.

    Joins by ``session_anchor == base_session_id``. Failure is non-fatal: rows
    keep their in-memory truth and just lack display metadata.
    """
    anchors = sorted({r["base_session_id"] for r in rows if r.get("base_session_id")})
    if not anchors:
        return
    try:
        from sqlalchemy import select

        from storage.db import create_sqlite_engine
        from storage.models import agent_sessions, scopes
    except Exception:  # noqa: BLE001
        return

    meta_by_anchor: dict[str, list[dict[str, Any]]] = {}
    try:
        # Runs controller-side (this aggregator is only reached from the
        # controller's internal server), so ``create_sqlite_engine()`` resolves
        # the same DB the controller writes. Outer-join ``scopes`` so platform /
        # scope_type / display_name come from the canonical columns rather than
        # only string-splitting ``scope_id``.
        engine = create_sqlite_engine()
        with engine.connect() as conn:
            result = conn.execute(
                select(
                    agent_sessions.c.id,
                    agent_sessions.c.session_anchor,
                    agent_sessions.c.scope_id,
                    agent_sessions.c.agent_backend,
                    agent_sessions.c.title,
                    agent_sessions.c.workdir,
                    agent_sessions.c.agent_name,
                    agent_sessions.c.last_active_at,
                    scopes.c.platform.label("scope_platform"),
                    scopes.c.scope_type.label("scope_scope_type"),
                    scopes.c.display_name.label("scope_display_name"),
                    scopes.c.native_type.label("scope_native_type"),
                )
                .select_from(agent_sessions.outerjoin(scopes, scopes.c.id == agent_sessions.c.scope_id))
                .where(agent_sessions.c.session_anchor.in_(anchors))
            ).mappings()
            for row in result:
                anchor = row["session_anchor"]
                meta_by_anchor.setdefault(anchor, []).append(dict(row))
    except Exception:  # noqa: BLE001
        logger.debug("running_agents: db enrichment failed", exc_info=True)
        return

    for r in rows:
        meta = _choose_session_meta(r, meta_by_anchor.get(r.get("base_session_id")) or [])
        if meta:
            _apply_session_meta(r, meta)


def _choose_session_meta(r: dict[str, Any], candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Pick display metadata for one running row.

    ``agent_sessions`` may contain separate rows for the same anchor under
    different backends. Prefer the row matching the live backend; fall back to the
    most recent row only when no backend match exists.
    """
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda row: (row.get("last_active_at") or "", row.get("id") or ""),
        reverse=True,
    )
    backend = str(r.get("backend") or "").strip()
    if backend:
        for row in ordered:
            if str(row.get("agent_backend") or "").strip() == backend:
                return row
    return ordered[0]


def _apply_session_meta(r: dict[str, Any], meta: dict[str, Any]) -> None:
    """Fold one ``agent_sessions``⟕``scopes`` row into a running-agent row.

    Pure (no I/O) so it can be unit-tested without a DB.
    """
    scope_id = meta.get("scope_id")
    # Canonical scopes columns win; fall back to splitting scope_id when the
    # scope row is missing (FK is nullable / SET NULL).
    split_platform, split_scope_type = _split_scope_id(scope_id)
    platform = meta.get("scope_platform") or split_platform
    # Private agent runs (``vibe agent run`` / one agent invoking another) are
    # NOT IM sessions: ``reserve_private_agent_session`` stamps the scope with a
    # PLACEHOLDER platform (the configured primary platform — slack/discord/…,
    # only "slack" when config is unreadable) but marks it
    # ``native_type="private_agent_run"``. So keying off ``native_type`` (not the
    # platform) catches these uniformly on every install. Without this guard the
    # row would be mislabeled as a real IM session. Treat it as an agent-initiated
    # internal run: no IM platform, ``trigger_source="agent"``, not chat-openable.
    is_private_run = meta.get("scope_native_type") == "private_agent_run"
    r["session_id"] = meta.get("id")
    r["title"] = meta.get("title")
    r["platform"] = None if is_private_run else platform
    r["scope_type"] = meta.get("scope_scope_type") or split_scope_type
    r["scope_display_name"] = meta.get("scope_display_name")
    r["agent_name"] = meta.get("agent_name")
    if not r.get("workdir"):
        r["workdir"] = meta.get("workdir")
    # Trigger source: agent-initiated for private agent runs; otherwise human.
    # (scheduled/watch/webhook/callback are only asserted when a harness run
    # reliably links them, which is not modeled here.)
    r["trigger_source"] = "agent" if is_private_run else "human"
    # Private agent runs are internal (``no_delivery``) — not user chat sessions —
    # so they are not openable. Any other persisted session IS openable in the
    # workbench Chat by its id, IM sessions included (mirrors how the Inbox /
    # sidebar navigate to ``/chat/<session_id>``).
    r["openable_in_chat"] = bool(meta.get("id")) and not is_private_run


async def _end_orphan_pid(pid: int) -> dict[str, Any]:
    """SIGTERM→SIGKILL a leaked Claude process, but ONLY after re-verifying it is
    an avibe-owned, still-alive, identity-matching registry entry — so a bogus or
    reused pid from the client can never make us kill an unrelated process."""
    try:
        from modules.agents.claude_process_reaper import (
            AVIBE_CLAUDE_SESSION_OWNER,
            _build_children_map,
            _descendant_pids,
            _load_owned_process_registry,
            _parse_ps_rows,
            _process_start_time,
            _reap_pid_set,
            _run_ps,
        )
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "reaper_unavailable"}
    registry = list(_load_owned_process_registry())
    record = None
    for r in registry:
        if getattr(r, "pid", None) == pid and getattr(r, "owner", None) == AVIBE_CLAUDE_SESSION_OWNER:
            record = r
            break
    if record is None:
        return {"ok": False, "error": "not_an_owned_process", "pid": pid}
    current = _process_start_time(pid)
    if current is None:
        return {"ok": False, "error": "process_not_alive", "pid": pid}
    recorded = getattr(record, "started_at", None)
    # FAIL CLOSED: refuse to kill unless identity is provable. A record with a
    # missing/non-numeric ``started_at`` (e.g. the registration-time ``ps`` failed)
    # cannot be distinguished from a reused pid, so we never SIGKILL it — matching
    # the read path's conservatism (``_collect_orphans`` skips such records too).
    if not isinstance(recorded, (int, float)):
        return {"ok": False, "error": "identity_unprovable", "pid": pid}
    if abs(current - recorded) >= 1.0:
        return {"ok": False, "error": "pid_reused", "pid": pid}
    # NOTE: a microscopic TOCTOU remains between this check and the signal (the pid
    # could exit + be reused in the gap); it's inherent to pid-based signaling and
    # negligible vs. the registration→kill window the start-time match already covers.
    #
    # Reap the orphan root TOGETHER WITH its descendants (e.g. node helper
    # processes), mirroring the background orphan sweep — otherwise killing only
    # the registered root makes the Running tab row disappear while surviving
    # children leak with no registry root a later kill could target. Apply the
    # SAME safety subtractions as the sweep so we never signal the avibe service
    # (or its tree) or a process owned by a DIFFERENT registered session. The
    # blocking ``ps`` read is offloaded so we don't stall the controller loop. If
    # it fails, fall back to the verified root pid alone (better than nothing).
    target_pids = {pid}
    try:
        loop = asyncio.get_running_loop()
        rows = _parse_ps_rows(await loop.run_in_executor(None, _run_ps))
        children = _build_children_map(rows)
        target_pids |= _descendant_pids(rows, pid, children)
        # Never reap the avibe service itself or anything under it.
        service_pid = os.getpid()
        target_pids -= {service_pid} | _descendant_pids(rows, service_pid, children)
        # Never reap a pid owned by a DIFFERENT registry entry (or its descendants):
        # only this verified orphan root + its own helpers are in scope.
        for other in registry:
            other_pid = getattr(other, "pid", None)
            if isinstance(other_pid, int) and other_pid > 0 and other_pid != pid:
                target_pids.discard(other_pid)
                target_pids -= _descendant_pids(rows, other_pid, children)
        # The verified root is always in scope regardless of the subtractions above.
        target_pids.add(pid)
    except Exception:  # noqa: BLE001
        logger.debug("end: orphan descendant expansion failed for %s", pid, exc_info=True)
        target_pids = {pid}
    killed = await _reap_pid_set(target_pids, terminate_timeout=2.0, logger=logger)
    return {"ok": killed > 0, "action": "killed_process", "pid": pid, "reaped_pids": sorted(target_pids)}


def _find_claude_composite_for_base(session_handler: Any, base_session_id: Optional[str]) -> Optional[str]:
    if not base_session_id:
        return None
    sessions = getattr(session_handler, "claude_sessions", {}) or {}
    for composite_key, client in _safe_items(sessions):
        client_base = getattr(client, "_vibe_runtime_base_session_id", None) or _base_from_composite(composite_key)
        if client_base == base_session_id:
            return composite_key
    return None


def _claude_pid_for(
    controller: "Controller", composite_key: Optional[str], base_session_id: Optional[str]
) -> Optional[int]:
    """Resolve the OS pid of a live Claude session (by composite key, else base), or
    ``None``. Used to reap the lingering CLI subprocess after the canonical stop
    disconnects the client — the pid is unresolvable once the client is gone."""
    session_handler = getattr(controller, "session_handler", None)
    if session_handler is None:
        return None
    ck = composite_key or _find_claude_composite_for_base(session_handler, base_session_id)
    sessions = getattr(session_handler, "claude_sessions", {}) or {}
    client = sessions.get(ck) if ck else None
    if client is None:
        return None
    try:
        from modules.agents.claude_process_reaper import get_claude_client_pid

        return get_claude_client_pid(client)
    except Exception:  # noqa: BLE001
        return None


async def _end_claude(controller: "Controller", composite_key: Optional[str], base_session_id: Optional[str]) -> dict[str, Any]:
    session_handler = getattr(controller, "session_handler", None)
    if session_handler is None:
        return {"ok": False, "error": "session_handler_unavailable"}
    ck = composite_key or _find_claude_composite_for_base(session_handler, base_session_id)
    sessions = getattr(session_handler, "claude_sessions", {}) or {}
    client = sessions.get(ck) if ck else None
    if client is None:
        return {"ok": False, "error": "session_not_live"}
    # Capture the OS pid BEFORE teardown — once the client disconnects its
    # transport is gone and the pid is no longer resolvable.
    pid: Optional[int] = None
    try:
        from modules.agents.claude_process_reaper import get_claude_client_pid

        pid = get_claude_client_pid(client)
    except Exception:  # noqa: BLE001
        pid = None
    # Interrupt any in-flight turn first (best-effort), then disconnect + free the
    # SDK client / subprocess via the same path idle-eviction uses.
    try:
        if hasattr(client, "interrupt"):
            await client.interrupt()
    except Exception:  # noqa: BLE001
        logger.debug("end: claude interrupt failed for %s", ck, exc_info=True)
    try:
        await session_handler.cleanup_session(ck)
    except Exception as exc:  # noqa: BLE001
        logger.warning("end: claude cleanup_session failed for %s: %s", ck, exc)
        return {"ok": False, "error": "cleanup_failed", "detail": str(exc)}
    # ``disconnect`` only closes the SDK transport; the Claude CLI subprocess can
    # linger (becoming an orphan the reaper would only collect on a later sweep),
    # so "End" wouldn't actually free the process. Reap it promptly if still alive.
    process_killed = False
    if isinstance(pid, int):
        try:
            from modules.agents.claude_process_reaper import _reap_pid_set

            process_killed = (await _reap_pid_set({pid}, terminate_timeout=2.0, logger=logger)) > 0
        except Exception:  # noqa: BLE001
            logger.debug("end: claude pid reap failed for %s", pid, exc_info=True)
    return {"ok": True, "action": "ended", "backend": "claude", "pid": pid, "process_killed": process_killed}


async def _end_codex(controller: "Controller", base_session_id: Optional[str]) -> dict[str, Any]:
    if not base_session_id:
        return {"ok": False, "error": "base_session_id_required"}
    agent = _get_agent(controller, "codex")
    if agent is None:
        return {"ok": False, "error": "codex_unavailable"}
    session_mgr = getattr(agent, "_session_mgr", None)
    turn_registry = getattr(agent, "_turn_registry", None)
    transports = getattr(agent, "_transports", {}) or {}
    if session_mgr is None or turn_registry is None:
        return {"ok": False, "error": "codex_registries_unavailable"}
    cwd = session_mgr.get_cwd(base_session_id)
    thread_id = session_mgr.get_thread_id(base_session_id)
    turn_id = turn_registry.get_active_turn(base_session_id)
    transport = transports.get(cwd) if cwd else None
    # Interrupt the active turn (the shared app-server transport stays up for
    # other sessions on the same cwd); then clear THIS session's thread/turn state.
    interrupted = False
    if turn_id and thread_id and transport is not None:
        try:
            await transport.send_request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            interrupted = True
        except Exception:  # noqa: BLE001
            logger.debug("end: codex turn/interrupt failed for %s", base_session_id, exc_info=True)
    try:
        # Fully remove the session's mappings (thread + cwd + session_key), not
        # just ``invalidate_thread`` (which preserves cwd/session_key) — otherwise
        # ``_collect_codex``'s ``all_base_sessions()`` still enumerates it and the
        # row never disappears from the Running tab.
        session_mgr.clear(base_session_id)
        turn_registry.clear_session(base_session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("end: codex clear failed for %s: %s", base_session_id, exc)
        return {"ok": False, "error": "clear_failed", "detail": str(exc)}
    # The app-server transport is shared per cwd. If THIS was the last session on
    # that cwd, stop it too so the codex process is actually freed (otherwise it
    # lingers with zero sessions); if other sessions still use it, leave it up.
    process_killed = False
    if cwd and transport is not None:
        remaining: list = []
        try:
            remaining = session_mgr.sessions_for_cwd(cwd)
        except Exception:  # noqa: BLE001
            remaining = []
        if not remaining:
            try:
                await transport.stop()
                transports.pop(cwd, None)
                last_activity = getattr(agent, "_transport_last_activity", None)
                if isinstance(last_activity, dict):
                    last_activity.pop(cwd, None)
                process_killed = True
            except Exception:  # noqa: BLE001
                logger.debug("end: codex transport stop failed for %s", cwd, exc_info=True)
    # ``interrupted`` is False when there was no active turn to stop (idle/stale):
    # the session state is still cleared, but the caller can tell nothing was
    # actively interrupted.
    return {
        "ok": True,
        "action": "ended",
        "backend": "codex",
        "interrupted": interrupted,
        "process_killed": process_killed,
    }


async def _end_opencode(controller: "Controller", base_session_id: Optional[str]) -> dict[str, Any]:
    if not base_session_id:
        return {"ok": False, "error": "base_session_id_required"}
    agent = _get_agent(controller, "opencode")
    if agent is None:
        return {"ok": False, "error": "opencode_unavailable"}
    active_requests = getattr(agent, "_active_requests", {}) or {}
    task = active_requests.get(base_session_id)
    # Best-effort remote abort (so the OpenCode server stops the run too), then
    # cancel the local polling task.
    session_mgr = getattr(agent, "_session_manager", None)
    try:
        get_req = getattr(session_mgr, "get_request_session", None)
        req_info = get_req(base_session_id) if callable(get_req) else None
        if req_info:
            server = await agent._get_server()
            await server.abort_session(req_info[0], req_info[1])
    except Exception:  # noqa: BLE001
        logger.debug("end: opencode remote abort failed for %s", base_session_id, exc_info=True)
    if task is not None and not task.done():
        task.cancel()
    return {"ok": True, "action": "ended", "backend": "opencode"}


async def _settle_workbench_turn(controller: "Controller", session_id: Optional[str]) -> Optional[dict[str, Any]]:
    """If a Workbench/chat turn is in flight for ``session_id``, stop it through
    ``SessionTurnManager.cancel`` so the turn FSM settles: it interrupts the
    backend, emits the terminal result, AND cancels the ``dispatch_turn`` task the
    Chat page is awaiting. Skipping this (and only doing the backend teardown
    below) would leave the chat session stuck "running" with sends queued.

    Returns a success/failure result when the Workbench manager owned the turn;
    returns ``None`` for IM/agent-run turns and when there is no turn owner.
    """
    if not session_id:
        return None
    manager = getattr(controller, "session_turns", None)
    if manager is None or not getattr(manager, "is_in_flight", None):
        return None
    try:
        if not manager.is_in_flight(session_id):
            return None
        result = await manager.cancel(session_id)
        if isinstance(result, dict) and result.get("ok"):
            return {
                "ok": True,
                "action": "stopped",
                "turn_settled": True,
                "stop_status": result.get("status"),
                "backend": result.get("backend"),
            }
        return {
            "ok": False,
            "error": (result or {}).get("code") or "stop_failed",
            "turn_settled": False,
            "detail": result,
        }
    except Exception:  # noqa: BLE001
        logger.debug("end: workbench turn cancel failed for %s", session_id, exc_info=True)
        return {"ok": False, "error": "stop_failed", "turn_settled": False}


def _workdir_from_composite(composite_key: Optional[str]) -> Optional[str]:
    if not composite_key:
        return None
    _base, sep, workdir = composite_key.rpartition(":")
    return workdir if sep and workdir else None


def _live_workdir_for_backend(
    controller: "Controller",
    backend: Optional[str],
    base_session_id: Optional[str],
    composite_key: Optional[str],
) -> Optional[str]:
    workdir = _workdir_from_composite(composite_key)
    if workdir or not base_session_id:
        return workdir
    if backend == "codex":
        agent = _get_agent(controller, "codex")
        session_mgr = getattr(agent, "_session_mgr", None)
        get_cwd = getattr(session_mgr, "get_cwd", None)
        if callable(get_cwd):
            try:
                return get_cwd(base_session_id)
            except Exception:  # noqa: BLE001
                logger.debug("end: failed to resolve codex cwd for %s", base_session_id, exc_info=True)
    elif backend == "opencode":
        agent = _get_agent(controller, "opencode")
        session_mgr = getattr(agent, "_session_manager", None)
        get_req = getattr(session_mgr, "get_request_session", None)
        if callable(get_req):
            try:
                req_info = get_req(base_session_id)
            except Exception:  # noqa: BLE001
                req_info = None
            if req_info and len(req_info) >= 2:
                return req_info[1]
    return None


def _build_stop_context(
    controller: "Controller",
    *,
    backend: Optional[str],
    session_id: Optional[str],
    composite_key: Optional[str],
    base_session_id: Optional[str],
) -> Any:
    manager = getattr(controller, "session_turns", None)
    build_context = getattr(manager, "_build_context", None)
    context = None
    if session_id and callable(build_context):
        try:
            context = build_context(session_id)
        except Exception:  # noqa: BLE001
            logger.debug("end: failed to rebuild stop context for %s", session_id, exc_info=True)
    if context is None:
        try:
            from modules.im import MessageContext
        except Exception:  # noqa: BLE001
            return None
        channel_id = session_id or base_session_id or composite_key or "running-agent"
        context = MessageContext(user_id="workbench", channel_id=str(channel_id), platform="avibe")
    if getattr(context, "platform_specific", None) is None:
        context.platform_specific = {}
    payload = context.platform_specific
    payload["suppress_stop_no_active_notice"] = True
    effective_base_session_id = base_session_id or (_base_from_composite(composite_key) if composite_key else None)
    if effective_base_session_id:
        payload["backend_base_session_id"] = effective_base_session_id
    if composite_key:
        payload["backend_composite_session_id"] = composite_key
    target = payload.get("agent_session_target")
    if not isinstance(target, dict):
        target = {}
        payload["agent_session_target"] = target
    if session_id and not target.get("id"):
        target["id"] = session_id
    if backend and not target.get("agent_backend"):
        target["agent_backend"] = backend
    if effective_base_session_id and not target.get("session_anchor"):
        target["session_anchor"] = effective_base_session_id
    workdir = _live_workdir_for_backend(controller, backend, effective_base_session_id, composite_key)
    if workdir and not target.get("workdir"):
        target["workdir"] = workdir
    return context


async def _stop_active_agent(
    controller: "Controller",
    *,
    backend: Optional[str],
    session_id: Optional[str],
    composite_key: Optional[str],
    base_session_id: Optional[str],
) -> dict[str, Any]:
    settled = await _settle_workbench_turn(controller, session_id)
    if settled is not None:
        if settled.get("backend") is None and backend:
            settled["backend"] = backend
        return settled

    handler = getattr(controller, "command_handler", None)
    handle_stop = getattr(handler, "handle_stop", None)
    if not callable(handle_stop):
        return {"ok": False, "error": "stop_unavailable"}
    context = _build_stop_context(
        controller,
        backend=backend,
        session_id=session_id,
        composite_key=composite_key,
        base_session_id=base_session_id,
    )
    if context is None:
        return {"ok": False, "error": "context_unavailable"}
    try:
        handled = bool(await handle_stop(context))
    except Exception:  # noqa: BLE001
        logger.debug("end: canonical stop failed for %s", base_session_id or session_id, exc_info=True)
        return {"ok": False, "error": "stop_failed"}
    if handled:
        return {"ok": True, "action": "stopped", "backend": backend, "turn_settled": False}
    payload = getattr(context, "platform_specific", None) or {}
    return {"ok": False, "error": str(payload.get("stop_failure_reason") or "stop_failed")}


def _inflight_turn_matches_row(
    controller: "Controller",
    *,
    session_id: str,
    backend: Optional[str],
    base_session_id: Optional[str],
) -> bool:
    """True when the Workbench turn in flight for ``session_id`` is THIS row's
    runtime (same backend / session anchor), so promoting the row to active and
    canceling that turn is correct.

    The in-flight turn's identity comes from the ``MessageContext`` it started
    under (``platform_specific.agent_session_target`` → ``agent_backend`` /
    ``session_anchor``; same source ``turn_state`` uses). We require a positive
    match on backend and/or anchor and NO conflict on either: a mismatch means a
    different turn is running under the same chat, and "no evidence" is treated as
    not-this-row (the backend-specific checks then decide), so we never cancel an
    unrelated turn.
    """
    manager = getattr(controller, "session_turns", None)
    in_flight = getattr(manager, "in_flight", None)
    if not isinstance(in_flight, dict):
        return False
    entry = in_flight.get(session_id)
    if entry is None:
        return False
    task = getattr(entry, "task", None)
    try:
        if task is not None and hasattr(task, "done") and task.done():
            return False
    except Exception:  # noqa: BLE001
        pass
    payload = getattr(getattr(entry, "context", None), "platform_specific", None) or {}
    target = payload.get("agent_session_target") if isinstance(payload, dict) else None
    target = target if isinstance(target, dict) else {}
    turn_backend = str(target.get("agent_backend") or "").strip()
    turn_anchor = str(target.get("session_anchor") or "").strip()
    row_backend = str(backend or "").strip()
    row_base = str(base_session_id or "").strip()

    backend_conflict = bool(turn_backend and row_backend and turn_backend != row_backend)
    anchor_conflict = bool(turn_anchor and row_base and turn_anchor != row_base)
    if backend_conflict or anchor_conflict:
        return False
    backend_match = bool(turn_backend and turn_backend == row_backend)
    anchor_match = bool(turn_anchor and turn_anchor == row_base)
    return backend_match or anchor_match


def _resolve_live_state(
    controller: "Controller",
    *,
    backend: Optional[str],
    session_id: Optional[str],
    composite_key: Optional[str],
    base_session_id: Optional[str],
) -> Optional[str]:
    """Recompute a target's CURRENT ``active``/``idle`` state from live registries.

    The browser sends the ``state`` it last polled, but a row can flip
    idle→active (or active→idle) before the user clicks End. Trusting the stale
    value would route an active turn down the idle teardown — which interrupts
    the turn without releasing the Workbench/AgentService runtime gate or
    emitting the terminal result, leaving the chat stuck. Re-deriving the state
    here lets ``end_running_agent`` always pick the correct path.

    Returns ``"active"``/``"idle"``, or ``None`` when the live state can't be
    determined (caller then falls back to the client-supplied ``state``).
    """
    # A Workbench/chat turn in flight promotes the row to active — but ONLY when
    # that in-flight turn actually belongs to the clicked row. A chat session can
    # hold an idle row for one backend/base while a *different* turn (e.g. a new
    # Claude message after an idle Codex row) runs under the same ``session_id``;
    # promoting on ``session_id`` alone would make the active End cancel that
    # unrelated turn. So compare the in-flight turn's backend / session anchor to
    # the target before trusting it; on conflict or no positive identity match,
    # fall through to the backend-specific live checks below.
    if session_id and _inflight_turn_matches_row(
        controller, session_id=session_id, backend=backend, base_session_id=base_session_id
    ):
        return "active"

    if backend == "claude":
        active = getattr(controller, "claude_active_sessions", set()) or set()
        session_handler = getattr(controller, "session_handler", None)
        ck = composite_key or _find_claude_composite_for_base(session_handler, base_session_id)
        if ck is None:
            return None
        return "active" if ck in active else "idle"

    if backend == "codex":
        agent = _get_agent(controller, "codex")
        registry = getattr(agent, "_turn_registry", None)
        if registry is None or not base_session_id:
            return None
        try:
            if registry.get_active_turn(base_session_id):
                return "active"
            if hasattr(registry, "has_pending_turn_start") and registry.has_pending_turn_start(base_session_id):
                return "active"
        except Exception:  # noqa: BLE001
            logger.debug("end: codex live-state check failed for %s", base_session_id, exc_info=True)
            return None
        return "idle"

    if backend == "opencode":
        agent = _get_agent(controller, "opencode")
        active_requests = getattr(agent, "_active_requests", {}) or {}
        task = active_requests.get(base_session_id) if base_session_id else None
        if task is None:
            return "idle"
        try:
            return "idle" if bool(task.done()) else "active"
        except Exception:  # noqa: BLE001
            return "active"

    return None


async def end_running_agent(
    controller: "Controller",
    *,
    backend: Optional[str] = None,
    state: Optional[str] = None,
    session_id: Optional[str] = None,
    composite_key: Optional[str] = None,
    base_session_id: Optional[str] = None,
    pid: Optional[int] = None,
) -> dict[str, Any]:
    """Terminate a running agent's LIVE runtime, dispatched by backend + state.

    - orphan → SIGTERM/SIGKILL the leaked (verified, avibe-owned) process.
    - claude → interrupt the turn + disconnect the SDK client + reap the subprocess.
    - codex  → interrupt the turn + clear the session mappings (+ stop the shared
      app-server when this was its last session).
    - opencode → abort the remote run + cancel the local polling task.

    For an ACTIVE turn, the stop goes through the canonical per-backend stop path
    (Workbench turns via ``SessionTurnManager.cancel``; IM / agent-run turns via
    ``command_handler.handle_stop``) so the runtime gate / pending requests /
    terminal result are released, then the leftover runtime is freed (codex session
    + transport, opencode task, claude subprocess) so the row clears instead of
    forcing a second Disconnect.

    Runs on the controller event loop (mutates loop-owned registries / awaits
    backend coroutines). There is deliberately NO self-protection: ending the
    current session / avibe's own runtime is allowed by design.
    """
    if state == "orphan":
        if not isinstance(pid, int):
            return {"ok": False, "error": "pid_required_for_orphan"}
        return await _end_orphan_pid(pid)

    # Re-derive the live active/idle state server-side: the client's ``state`` is
    # from its last poll and may be stale (a row can flip idle→active before the
    # user clicks End). The recomputed value decides the teardown path so an
    # active turn never falls through to the idle path (which would skip the
    # canonical stop / gate + terminal-result release).
    live_state = _resolve_live_state(
        controller,
        backend=backend,
        session_id=session_id,
        composite_key=composite_key,
        base_session_id=base_session_id,
    )
    if live_state is not None:
        state = live_state

    if state == "active":
        # Capture the Claude OS pid BEFORE the stop disconnects the client — once
        # the SDK client is gone the pid is no longer resolvable.
        claude_pid = (
            _claude_pid_for(controller, composite_key, base_session_id) if backend == "claude" else None
        )
        stop_result = await _stop_active_agent(
            controller,
            backend=backend,
            session_id=session_id,
            composite_key=composite_key,
            base_session_id=base_session_id,
        )
        stop_ok = bool(stop_result.get("ok"))
        # The canonical stop interrupts the turn (and releases its runtime gate via
        # the turn's own context — verified in Incus) but does NOT free the rest of
        # the runtime. Finish the teardown so the row clears instead of forcing a
        # second Disconnect.
        if backend == "codex":
            # Tear down even when the stop FAILED: a stale-active row whose turn can
            # no longer be interrupted (app-server died) must still be clearable via
            # End, otherwise it sticks in the tab forever.
            teardown = await _end_codex(controller, base_session_id)
            if isinstance(teardown, dict) and teardown.get("ok"):
                result = dict(stop_result) if stop_ok else {"ok": True, "action": "ended", "backend": "codex"}
                if teardown.get("process_killed"):
                    result["process_killed"] = True
                return result
            return stop_result if stop_ok else (teardown if isinstance(teardown, dict) else stop_result)

        if not stop_ok:
            return stop_result
        if backend == "opencode":
            await _end_opencode(controller, base_session_id)
        elif backend == "claude" and isinstance(claude_pid, int):
            # Claude's client is already removed by the stop path (so its row clears,
            # and calling _end_claude here would wrongly report ``session_not_live``);
            # only the leftover CLI subprocess needs reaping.
            try:
                from modules.agents.claude_process_reaper import _reap_pid_set

                if (await _reap_pid_set({claude_pid}, terminate_timeout=2.0, logger=logger)) > 0:
                    stop_result["process_killed"] = True
            except Exception:  # noqa: BLE001
                logger.debug("end: claude reap after stop failed for %s", claude_pid, exc_info=True)
        return stop_result

    if backend == "claude":
        result = await _end_claude(controller, composite_key, base_session_id)
    elif backend == "codex":
        result = await _end_codex(controller, base_session_id)
    elif backend == "opencode":
        result = await _end_opencode(controller, base_session_id)
    elif isinstance(pid, int):
        # Fallback: a pid-only target with no backend is treated as an orphan kill.
        return await _end_orphan_pid(pid)
    else:
        return {"ok": False, "error": "unknown_target"}

    return result


def snapshot_running_agents(controller: "Controller") -> dict[str, Any]:
    """Return a read-only snapshot of all currently-running agent instances.

    Shape::

        {
          "ok": True,
          "agents": [ { backend, state, base_session_id, platform, scope_type,
                        scope_display_name, title, workdir, pid, pid_shared,
                        native_session_id, model, elapsed_seconds, trigger_source,
                        session_id, agent_name, openable_in_chat }, ... ],
          "counts": { "total", "active", "idle", "orphan",
                      "by_backend": {claude, codex, opencode} },
        }

    One row per live SESSION (F1): a Codex pid shared by several sessions yields
    one row per session, each flagged ``pid_shared``.
    """
    # ``now`` is MONOTONIC: only valid against the controller's monotonic
    # activity baselines (claude/codex). The orphan path deliberately uses a
    # separate wall-clock baseline (``time.time()`` vs ``ps`` start time) — do
    # NOT feed this ``now`` into orphan elapsed math.
    now = time.monotonic()
    seen_native: dict[str, Optional[int]] = {}
    seen_pids: set[int] = set()
    rows: list[dict[str, Any]] = []
    rows.extend(_collect_claude(controller, now, seen_native, seen_pids))
    rows.extend(_collect_codex(controller))
    rows.extend(_collect_opencode(controller))
    rows.extend(_collect_orphans(seen_native, seen_pids))

    _enrich_from_db(rows)

    # Single pass over rows for both the state tallies and the per-backend counts.
    states: dict[str, int] = {"active": 0, "idle": 0, "orphan": 0}
    by_backend: dict[str, int] = {}
    for r in rows:
        states[r["state"]] = states.get(r["state"], 0) + 1
        by_backend[r["backend"]] = by_backend.get(r["backend"], 0) + 1

    return {
        "ok": True,
        "agents": rows,
        "counts": {
            "total": len(rows),
            "active": states["active"],
            "idle": states["idle"],
            "orphan": states["orphan"],
            "by_backend": by_backend,
        },
    }
