"""One entry point every run-blind session teardown calls before dismantling anything.

WHY THIS MODULE EXISTS. Avibe reclaims sessions from a lot of places — idle
eviction, the stuck-active backstop, a broken Claude transport, a Codex transport
going idle, controller shutdown, the Running tab's End button. Each of them used to
tear the runtime down and say nothing about the runs executing inside it, so a run
stayed ``running`` forever with no backend left to settle it, and its session lock
stayed held so the next drain could not dispatch either.

The fix is not a flag on each of those paths; it is one ordering, stated once:

    record the cause -> cancel both lanes (awaited) -> reconcile the session's rows
    -> tear the backend down

Settle first, tear down second. The reverse order is the bug: a torn-down backend
can no longer settle its own turn.

WHY A MODULE AND NOT DIRECT CALLS. The teardown entries live in four different
layers — a platform-agnostic handler, a Web-UI service, the controller, and a
backend adapter — and none of them should have to know how to find the settlement
service, or what to do when it is absent. Everything here is defensive by
construction: a controller with no scheduled task service (headless runs, the many
``SimpleNamespace`` doubles in the test suite) is a no-op, never an exception on a
path that is already tearing something down. A teardown that fails because its
settlement bookkeeping raised would be strictly worse than the bug being fixed.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "cancel_session_scheduler_lane",
    "reconcile_session_runs",
    "resolve_teardown_session_ids",
    "split_composite_session_key",
    "teardown_composite_session_runs",
    "teardown_runtime_session_runs",
    "teardown_session_runs",
]


def split_composite_session_key(composite_key: Optional[str]) -> tuple[str, str]:
    """Split ``f"{session_anchor}:{working_path}"`` back into its two halves.

    ``rpartition`` rather than ``partition``: an anchor may itself contain colons
    (``slack::channel::C123``-shaped keys do), while the working path is appended
    last, so only the FINAL separator is the one this key added. Returns
    ``(anchor, "")`` for a key with no separator at all.
    """

    resolved = str(composite_key or "").strip()
    if not resolved:
        return "", ""
    anchor, separator, working_path = resolved.rpartition(":")
    if not separator:
        return resolved, ""
    return anchor, working_path


def _scheduled_task_service(controller: Any) -> Any:
    service = getattr(controller, "scheduled_task_service", None)
    if service is None:
        return None
    if not callable(getattr(service, "teardown_session_runs", None)):
        return None
    return service


def resolve_teardown_session_ids(
    controller: Any,
    *,
    session_anchor: str,
    workdir: Optional[str] = None,
    agent_backend: Optional[str] = None,
) -> list[str]:
    """Resolve a RUNTIME identity to the Avibe session ids a teardown must settle.

    The runtime knows anchors and working directories; ``agent_runs.session_id`` and
    the scheduler's session lock cache are both keyed by the ``agent_sessions`` row
    id. This is the hop between them (the two-hop resolve §3.3 of the plan calls
    for), and it is deliberately the only place that hop is spelled: eviction, Codex
    transport eviction and End all start from a different runtime identity but need
    the same answer.

    Returns a LIST because ``(scope, anchor)`` is the unique key, not the anchor
    alone. Normally one id; empty when the anchor names no live session, which is the
    common and cheap case (an IM session with no harness runs).

    ``agent_backend`` is the runtime's OWN backend, and every caller has it because
    every caller is a specific runtime being reclaimed. It is passed down as a
    predicate rather than used to pick a winner afterwards: an ambiguous list is not
    inspected here, it is CANCELLED, so the narrowing has to happen before the rows
    become ids.
    """

    anchor = str(session_anchor or "").strip()
    if not anchor:
        return []
    sessions = getattr(controller, "sessions", None)
    finder = getattr(sessions, "find_session_ids_for_anchor", None)
    if not callable(finder):
        return []
    try:
        resolved = finder(
            anchor,
            workdir=workdir or None,
            agent_backend=str(agent_backend or "").strip() or None,
        )
    except Exception:
        logger.debug(
            "Session teardown: could not resolve session ids for anchor %s",
            anchor,
            exc_info=True,
        )
        return []
    return [str(session_id) for session_id in (resolved or []) if session_id]


async def teardown_session_runs(
    controller: Any,
    session_id: str,
    *,
    settled_by: str,
    include_manager_lane: bool = True,
) -> int:
    """Cancel and settle everything running in one session. Returns what it touched.

    Thin, forgiving wrapper over ``ScheduledTaskService.teardown_session_runs`` — the
    method owns the cancel/await/reconcile ordering; this owns "find the service, and
    never let its absence or failure break a teardown".
    """

    resolved = str(session_id or "").strip()
    if not resolved:
        return 0
    service = _scheduled_task_service(controller)
    if service is None:
        return 0
    try:
        result = await service.teardown_session_runs(
            resolved,
            settled_by=settled_by,
            include_manager_lane=include_manager_lane,
        )
    except Exception:
        logger.warning(
            "Session teardown: settling runs for session %s as %s failed",
            resolved,
            settled_by,
            exc_info=True,
        )
        return 0
    return int(getattr(result, "cancelled_count", 0)) + int(
        getattr(result, "reconciled_count", 0)
    )


async def cancel_session_scheduler_lane(
    controller: Any,
    session_id: str,
    *,
    settled_by: str,
) -> frozenset[str]:
    """Cancel only the scheduler lane, and hand back the pre-cancel ownership snapshot.

    For the ONE caller that owns the manager lane itself: the Running tab's End runs
    the canonical user-Stop path for the turn, which must keep recording ``stopped``
    and keep the backend's own Stop behaviour. Cancelling the turn here first would
    leave that path with nothing to stop and degrade a successful End into an error.

    The snapshot has to cross the caller's own stop, which is why it is returned
    rather than consumed here: the reconcile that follows the stop needs the ownership
    that existed BEFORE any of it began, and by then every map that recorded it has
    been cleared.
    """

    resolved = str(session_id or "").strip()
    if not resolved:
        return frozenset()
    service = getattr(controller, "scheduled_task_service", None)
    canceller = getattr(service, "cancel_session_executions", None)
    if not callable(canceller):
        return frozenset()
    try:
        result = await canceller(
            resolved, settled_by=settled_by, include_manager_lane=False
        )
    except Exception:
        logger.warning(
            "Session teardown: scheduler-lane cancellation for session %s as %s failed",
            resolved,
            settled_by,
            exc_info=True,
        )
        return frozenset()
    return frozenset(getattr(result, "claimed_run_ids", frozenset()))


async def reconcile_session_runs(
    controller: Any,
    session_id: str,
    *,
    settled_by: str,
    claimed_run_ids: "frozenset[str] | set[str]",
) -> int:
    """Run the session-scoped reconcile alone, for a caller that cancelled in pieces.

    Companion to :func:`cancel_session_scheduler_lane`. Both halves exist separately
    only because End interleaves its own stop between them; every other entry uses
    :func:`teardown_session_runs`, which keeps the two adjacent and in order.
    """

    resolved = str(session_id or "").strip()
    if not resolved or not claimed_run_ids:
        return 0
    service = getattr(controller, "scheduled_task_service", None)
    reconciler = getattr(service, "reconcile_session_teardown", None)
    if not callable(reconciler):
        return 0
    try:
        return int(
            reconciler(resolved, settled_by=settled_by, claimed_run_ids=claimed_run_ids)
        )
    except Exception:
        logger.warning(
            "Session teardown: reconciling session %s as %s failed",
            resolved,
            settled_by,
            exc_info=True,
        )
        return 0


async def teardown_runtime_session_runs(
    controller: Any,
    *,
    session_anchor: str,
    workdir: Optional[str] = None,
    agent_backend: Optional[str] = None,
    settled_by: str,
    include_manager_lane: bool = True,
) -> int:
    """:func:`teardown_session_runs` for callers that hold a runtime identity.

    Resolves the anchor (plus working dir and backend, when known) to session ids and
    settles each one. Every id the resolve returns is torn down rather than just the
    newest: a genuinely ambiguous anchor gives no way to pick, and the reconciler's
    ownership intersection makes a wrong guess inert on the RECONCILE leg — it can
    only fail to find rows, never settle a run this process did not claim.

    THAT PROTECTION DOES NOT COVER THE CANCEL LEG, which is why the resolve must be
    narrow rather than generous. ``cancel_session_executions`` and
    ``release_for_teardown`` actively interrupt whatever each candidate is running,
    with no ownership intersection in front of them: a foreign scope's live turn gets
    cancelled by another scope's eviction. ``agent_backend`` — which every runtime
    caller knows about itself — keeps the candidate set to sessions this runtime could
    plausibly own.
    """

    session_ids = resolve_teardown_session_ids(
        controller,
        session_anchor=session_anchor,
        workdir=workdir,
        agent_backend=agent_backend,
    )
    touched = 0
    for session_id in session_ids:
        touched += await teardown_session_runs(
            controller,
            session_id,
            settled_by=settled_by,
            include_manager_lane=include_manager_lane,
        )
    return touched


async def teardown_composite_session_runs(
    controller: Any,
    composite_key: Optional[str],
    *,
    settled_by: str,
    agent_backend: Optional[str] = None,
    include_manager_lane: bool = True,
) -> int:
    """:func:`teardown_runtime_session_runs` keyed by a Claude composite session key."""

    anchor, working_path = split_composite_session_key(composite_key)
    if not anchor:
        return 0
    return await teardown_runtime_session_runs(
        controller,
        session_anchor=anchor,
        workdir=working_path,
        agent_backend=agent_backend,
        settled_by=settled_by,
        include_manager_lane=include_manager_lane,
    )
